"""HTTPS only, verified against the public certificate authorities.

Standard library only, so `deploy.py` (urllib) and `client.py` (requests) share
it without either one pulling in the other's stack.

Two rules live here and nowhere else, and neither has an off switch -- no
setting, flag, environment variable or config value relaxes them:

1. **The server URL is `https://`, always.** This client sends medical images
   and a bearer token, and the server has no plain-HTTP listener at all. Any
   `http://` URL is refused, whatever the host, loopback included: nothing is
   upgraded behind the user's back, and an `http://` URL saved by an older
   client is refused with the same message.

2. **The certificate is verified against the public authorities**, the
   `certifi` bundle for `requests` and the system store for urllib, with the
   hostname check on. There is no custom CA, no pinning and never
   `verify=False`. The server is therefore reached through the name its
   publicly-signed certificate is issued for -- the
   `https://<name>.<tailnet>.ts.net` address `tailscale serve` publishes --
   including when it runs on this very machine: the self-signed certificate
   the server presents on its own port is not one this client accepts.
"""

import ipaddress
import ssl
import urllib.request
from urllib.parse import urlsplit

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


def urlopen(url: str, timeout: float):
    """urllib.request.urlopen, https:// only, verified against the system's
    public certificate authorities with the hostname check on."""
    url = require_https(url)
    return urllib.request.urlopen(url, timeout=timeout, context=ssl.create_default_context())
