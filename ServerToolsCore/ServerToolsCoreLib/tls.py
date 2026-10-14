"""HTTPS only, and how a loopback server's own certificate is trusted.

Standard library only, so `deploy.py` (urllib) and `client.py` (requests) share
it without either one pulling in the other's stack.

Two rules live here and nowhere else:

1. **The server URL is `https://`, always.** This client sends medical images
   and a bearer token, and the server has no plain-HTTP listener at all, so
   there is no setting, flag or environment variable that relaxes this. A
   loopback `http://` URL saved by an older client is upgraded in place (the
   server now serves HTTPS on that very port); any other `http://` URL is
   refused.

2. **A loopback server is verified against the certificate it presents.** The
   local deployment generates its own self-signed certificate, which no public
   authority has signed. Instead of turning verification off, the client reads
   that certificate over loopback and verifies against it alone, hostname check
   included (its SAN covers `localhost`, `127.0.0.1` and `::1`).

   Trust on first use is acceptable ONLY because the host is loopback: the
   bytes of the certificate never leave this machine, and a process able to
   bind that port here already runs as a user who can read the very files
   being sent. For any other host the same trick would let whoever answers
   first on the network vouch for itself, so a remote host keeps
   `config.VERIFY_TLS` untouched (True, or the CA path a maintainer set).
"""

import atexit
import ipaddress
import logging
import os
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Union
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

HTTPS_REQUIRED_MESSAGE = (
    "The server URL must start with https://; this server only speaks HTTPS "
    "and medical images are never sent unencrypted."
)

_FETCH_TIMEOUT = 5.0

_lock = threading.Lock()
# (host, port) -> path of the PEM file holding that server's certificate.
_pins = {}


class InsecureServerUrl(ValueError):
    """The server URL is not https:// and cannot be upgraded to it."""

    def __init__(self, message: str = HTTPS_REQUIRED_MESSAGE):
        super().__init__(message)


def is_loopback(host) -> bool:
    """Whether `host` names this machine, by name or by address.

    The one loopback predicate of the client. `127.0.0.1` is not the only
    loopback address: the whole `127.0.0.0/8` is, and so is `::1`, which
    arrives from a URL wrapped in brackets, and any `*.localhost` name.
    The local server's certificate names only `localhost`, `127.0.0.1` and
    `::1`; another loopback name is still pinned, and fails the hostname
    check rather than being trusted.
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
    """Return `url` as an https:// URL without a trailing slash, or raise.

    A loopback `http://` URL becomes `https://` on the same port: that is
    exactly what the local server serves there now. Anything else that is not
    `https://` raises InsecureServerUrl.
    """
    text = (url or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise InsecureServerUrl() from None
    scheme = parts.scheme.lower()
    if not host:
        raise InsecureServerUrl()
    if scheme == "https":
        return "https" + text[len(parts.scheme):]
    if scheme == "http" and is_loopback(host):
        netloc = parts.netloc if port is not None else f"{parts.netloc}:80"
        return f"https://{netloc}{text[len(parts.scheme) + 3 + len(parts.netloc):]}"
    raise InsecureServerUrl()


def _host_port(url: str):
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    return parts.hostname, parts.port or 443


def _fetch_certificate(host: str, port: int) -> str:
    try:
        return ssl.get_server_certificate((host, port), timeout=_FETCH_TIMEOUT)
    except TypeError:  # Python < 3.10 has no `timeout`
        return ssl.get_server_certificate((host, port))


def _write_private(pem: str) -> str:
    # mkstemp creates the file 0600, readable by this user alone.
    descriptor, path = tempfile.mkstemp(prefix="visor-local-server-", suffix=".pem")
    with os.fdopen(descriptor, "w") as handle:
        handle.write(pem)
    atexit.register(_unlink_quietly, path)
    return path


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def pinned_certificate(host: str, port: int, refresh: bool = False):
    """Path of a PEM file holding the certificate a LOOPBACK server presents.

    Fetched once per (host, port) per process; `refresh` fetches it again, for
    a server that regenerated its certificate since. None when the server
    cannot be reached, which is left for the request itself to report.
    """
    if not is_loopback(host):
        raise ValueError("certificate pinning is only for loopback hosts")
    key = (host.strip("[]").lower(), int(port))
    with _lock:
        if not refresh and key in _pins:
            return _pins[key]
        try:
            pem = _fetch_certificate(key[0], key[1])
        except (OSError, ssl.SSLError) as exc:
            logger.debug("Could not read the local server's certificate: %s", exc)
            return None
        stale = _pins.get(key)
        _pins[key] = _write_private(pem)
        if stale:
            _unlink_quietly(stale)
        return _pins[key]


def verify_for(url: str, configured: Union[bool, str] = True, refresh: bool = False):
    """The `verify=` value for a request to `url`. Never False.

    `configured` is config.VERIFY_TLS: True, or the path of a CA file. A False
    there (a hand edit) is ignored: certificate verification is not optional on
    a client that sends medical images.
    """
    if configured is False:
        logger.warning("Certificate verification cannot be turned off; it stays on.")
        configured = True
    if configured is not True:
        return configured
    target = _host_port(url)
    if target and is_loopback(target[0]):
        pinned = pinned_certificate(target[0], target[1], refresh=refresh)
        if pinned:
            return pinned
    return True


def ssl_context_for(url: str, configured: Union[bool, str] = True, refresh: bool = False) -> ssl.SSLContext:
    """The same decision as verify_for, as an SSLContext for urllib."""
    verify = verify_for(url, configured, refresh=refresh)
    if verify is True:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=verify)


def is_certificate_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError)


def urlopen(url: str, timeout: float, configured: Union[bool, str] = True):
    """urllib.request.urlopen over HTTPS with the loopback pin, re-reading the
    certificate once if the server presents a different one than remembered."""
    url = require_https(url)
    try:
        return urllib.request.urlopen(url, timeout=timeout, context=ssl_context_for(url, configured))
    except urllib.error.URLError as exc:
        target = _host_port(url)
        if not (is_certificate_error(exc) and target and is_loopback(target[0])):
            raise
    return urllib.request.urlopen(
        url, timeout=timeout, context=ssl_context_for(url, configured, refresh=True)
    )
