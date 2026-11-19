"""HTTPS only, verified against the public certificate authorities.

Standard library only, `certifi` aside (imported when first needed), so `deploy.py` (urllib) and `client.py` (requests) share
it without either one pulling in the other's stack.

Three rules live here and nowhere else, and none has an off switch -- no
setting, flag, environment variable or config value relaxes them:

1. **The server URL is `https://`, always.** This client sends medical images
   and a bearer token. Any `http://` URL is refused, whatever the host,
   loopback included: nothing is upgraded behind the user's back, and an
   `http://` URL saved by an older client is refused with the same message.

2. **The certificate is verified against the public authorities**, the
   `certifi` bundle, named explicitly (`ca_bundle()`) for `requests` and for
   urllib alike, with the hostname check on. There is no custom CA, no
   trust-on-first-use and never `verify=False` -- and not `verify=True` either,
   which would let `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` in the environment
   swap the trusted authorities for any others. A server is therefore reached
   through the name its publicly-signed certificate is issued for: the
   `https://<name>.<tailnet>.ts.net` address `tailscale serve` publishes, or
   its own domain behind a reverse proxy or served directly with a Let's
   Encrypt or institutional certificate.

3. **The one exception: a server on this very machine, pinned out of band.**
   For `127.0.0.1` and `::1` only, and only when the file
   `~/.slicercloud/tls/server.crt` exists and passes `pinned_certificate()`,
   the connection is verified against THAT certificate instead of the public
   bundle. The file is written by SlicerCloud on this machine, together with
   the private key the local server is started with; it is never fetched from
   a server, never learned from a handshake (no trust on first use), and a
   missing or suspicious file means rule 2 applies unchanged.
"""

import ipaddress
import os
import re
import ssl
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

#: The only hosts the out-of-band pin applies to. Exactly these two literal
#: addresses, not the whole 127.0.0.0/8, and no name (not `localhost`: a name
#: is resolved through the hosts file): they are what the pinned certificate's
#: SAN lists (see SlicerCloudLib/local_cert.py).
LOCAL_PIN_HOSTS = ("127.0.0.1", "::1")
LOCAL_CERT_NAME = "server.crt"
LOCAL_KEY_NAME = "server.key"
_PIN_MAX_BYTES = 16 * 1024

HTTPS_REQUIRED_MESSAGE = (
    "The server URL must start with https://; this server only speaks HTTPS "
    "and medical images are never sent unencrypted."
)


class InsecureServerUrl(ValueError):
    """The server URL is not https://."""

    def __init__(self, message: str = HTTPS_REQUIRED_MESSAGE):
        super().__init__(message)


def is_loopback(host) -> bool:
    """Whether `host` names this machine, by name or by address.

    The client's one loopback predicate. `127.0.0.1` is not the only loopback
    address: the whole `127.0.0.0/8` is, and so is `::1`, which arrives from a
    URL wrapped in brackets, and any `*.localhost` name. It decides nothing
    about transport security -- loopback is held to the same https:// rule --
    only whether the data leaves the machine (see settings_qt).
    """
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_https(url: str) -> str:
    """Return `url` without a trailing slash, or raise InsecureServerUrl.

    Anything but `https://<host>` is refused, `http://localhost` included.
    """
    text = (url or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise InsecureServerUrl() from None
    if parts.scheme.lower() != "https" or not host:
        raise InsecureServerUrl()
    return "https" + text[len(parts.scheme):]


def local_tls_dir() -> str:
    """Where SlicerCloud keeps the local server's certificate and private key.

    A fixed place under the user's home. On POSIX the home comes from the
    account database, not from `$HOME`, so a variable cannot redirect where the
    pin is looked for. Tests patch this function.
    """
    home = os.path.expanduser("~")
    if os.name == "posix":
        try:
            import pwd
            home = pwd.getpwuid(os.geteuid()).pw_dir or home
        except (ImportError, KeyError):
            pass
    return os.path.join(home, ".slicercloud", "tls")


def _owned_and_private(info) -> bool:
    """POSIX: owned by the current user and not writable by group or others.
    Windows has no such mode bits; its per-user profile is the protection."""
    if os.name == "nt":
        return True
    return info.st_uid == os.geteuid() and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


_PEM_BLOCK = re.compile(
    r"^-----BEGIN CERTIFICATE-----\n((?:[A-Za-z0-9+/=]+\n)+)-----END CERTIFICATE-----$", re.MULTILINE
)


def _single_certificate_block(text: str):
    """The one `CERTIFICATE` PEM block of `text`, or None. Every `-----`
    marker of the file must belong to that block: a TRUSTED CERTIFICATE, X509
    CERTIFICATE or key block anywhere, even after a well-formed one, is
    refused, since OpenSSL loads some of those as trust anchors. Lines outside
    the block must be `#` comments."""
    text = text.replace("\r\n", "\n")
    found = list(_PEM_BLOCK.finditer(text))
    if len(found) != 1 or text.count("-----BEGIN ") != 1 or text.count("-----END ") != 1:
        return None
    block = found[0]
    outside = text[: block.start()] + text[block.end():]
    if "-----" in outside:
        return None
    for line in outside.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return None
    return block.group(0)


def pinned_certificate(host):
    """The path of the certificate to verify `host` against, or None.

    None means "no pin applies": the caller verifies against the public
    authorities, which is the rule for every host but this one. A path comes
    back only when ALL of these hold:

    - `host` is one of `LOCAL_PIN_HOSTS`, literally;
    - the certificate file and its folder are real (not symlinks), owned by
      the current user and not writable by anyone else, so another account on
      this machine cannot plant or swap the pin;
    - the file holds exactly one PEM block, of type CERTIFICATE exactly (no
      key, no TRUSTED CERTIFICATE, no X509 CERTIFICATE, no second block), and
      nothing else outside it but `#` comment lines, so it cannot be a
      bundle that adds trust anchors.

    That the certificate actually matches the server is then checked by the TLS
    handshake itself: the pin is the only trust anchor, the SAN must name the
    address connected to and the validity dates apply. A different certificate,
    an expired one, or a squatter on the port without the private key fails
    there.
    """
    if (host or "").strip("[]").lower() not in LOCAL_PIN_HOSTS:
        return None
    directory = local_tls_dir()
    path = os.path.join(directory, LOCAL_CERT_NAME)
    try:
        folder_info, file_info = os.lstat(directory), os.lstat(path)
        if not (stat.S_ISDIR(folder_info.st_mode) and stat.S_ISREG(file_info.st_mode)):
            return None
        if not (_owned_and_private(folder_info) and _owned_and_private(file_info)):
            return None
        if file_info.st_size > _PIN_MAX_BYTES:
            return None
        with open(path, "rb") as handle:
            text = handle.read().decode("ascii")
        block = _single_certificate_block(text)
        if block is None:
            return None
        ssl.PEM_cert_to_DER_cert(block)
    except (OSError, ValueError):
        return None
    return path


def pin_for_url(url: str):
    """`pinned_certificate` for the host of an https:// `url`; None otherwise."""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    return pinned_certificate(host)


def ca_bundle() -> str:
    """The public certificate authorities every connection is verified
    against: certifi's bundle, the one `requests` ships. Passed explicitly,
    never left to `verify=True`, so no environment variable can replace it.
    Imported here rather than at the top so the module stays importable where
    only the standard library is."""
    import certifi

    return certifi.where()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Every redirect is an error. A redirect is how https:// becomes http://
    without the new URL ever passing require_https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are never followed", headers, fp
        )


class _RefusePlainHttp(urllib.request.HTTPHandler):
    def http_open(self, req):
        raise urllib.error.URLError(HTTPS_REQUIRED_MESSAGE)


def _opener(context: ssl.SSLContext):
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _RefuseRedirects(), _RefusePlainHttp()
    )


def urlopen(url: str, timeout: float):
    """urllib's urlopen, https:// only, never following a redirect, verified
    against the public certificate authorities with the hostname check on --
    or, for this machine's own address only, against the pinned local
    certificate (rule 3 above)."""
    url = require_https(url)
    cafile = pin_for_url(url) or ca_bundle()
    return _opener(ssl.create_default_context(cafile=cafile)).open(url, timeout=timeout)
