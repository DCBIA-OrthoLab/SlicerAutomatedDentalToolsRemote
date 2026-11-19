"""Whether a tool panel can work at all, and what to offer when it cannot.

**This module imports neither `slicer` nor `qt`.** Every tool module is a
`ServerToolWidgetBase`; asking this module once, from the base class, covers all
of them. A module that is opened before any server is set used to show its
whole form, an error line, and a red badge: the person had to guess that the
fix lives in another module. Now the panel is replaced by a short card that
says what is missing and offers the two ways out.

The answer comes from the configured address only, never from the network: the
placeholder host (`.invalid`) is recognised by its name, so no DNS query is made
for a name that cannot exist.

    OK        a server is configured: the panel is shown as it always was
    UNSET     nothing is configured: the card replaces the panel
    LOCAL     the address is this machine (a server started by VISOR Local)

Starting a server automatically is a separate, narrow rule, `may_autostart`:
only for an address on THIS machine whose server does not answer, and only when
a local server is installed. A remote address, configured on purpose, is never
touched and nothing is started because of it.

The module that knows how to start the local server (SlicerCloud) registers a
provider here at startup; this package does not import it, so it stays usable
when SlicerCloud is absent.
"""

import threading
from urllib.parse import urlsplit

from .client import _never_resolves
from .tls import is_loopback

OK = "ok"
UNSET = "unset"
LOCAL = "local"

#: Texts, in one place so a test can read what a person reads.
TITLE_UNSET = "No server is set"
BODY_UNSET = (
    "These tools do their computing on a server. Connect to one that someone "
    "runs for you, or run it on this computer."
)
CONNECT = "Connect to a server"
COMPUTE_HERE = "Compute on this computer"
START_LOCAL = "Start VISOR Local"
BODY_INSTALLED = (
    "VISOR Local is already set up on this computer but it is not running. "
    "Start it to use these tools."
)
TITLE_STARTING = "Starting VISOR Local"
BODY_STARTING = "VISOR Local was stopped when Slicer last closed. It is starting again, this can take a minute."
TITLE_FAILED = "VISOR Local did not start"
BODY_FAILED = "{reason}\n\nOpen VISOR Local to see what is wrong, or try again."
OPEN_LOCAL = "Open VISOR Local"
TRY_AGAIN = "Try again"


def classify(server_url) -> str:
    """UNSET, LOCAL or OK, from the address alone (no network, no DNS)."""
    if not server_url:
        return UNSET
    try:
        if _never_resolves(str(server_url)):
            return UNSET
        host = urlsplit(str(server_url)).hostname or ""
    except ValueError:
        # A malformed address is a server somebody typed, not a missing one:
        # the settings panel reports it, and nothing is started for it.
        return OK
    return LOCAL if is_loopback(host) else OK


def may_autostart(server_url, health_ok, local_installed) -> bool:
    """True only for: this machine's address, no answer, a local install.

    Never for a remote address (even an unreachable one: the person chose it),
    and never for the placeholder: nothing was configured, so nothing is
    started on someone's behalf. That case shows the card and a Start button.
    """
    if health_ok or not local_installed:
        return False
    return classify(server_url) == LOCAL


class LocalServer:
    """What the card needs from the module that manages the local server.

    A provider implements `snapshot()`, which runs on the main thread and
    returns a `LocalServer`; `installed()` and `start()` then run on a worker
    thread and must not touch Qt.
    """

    def installed(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def start(self) -> dict:  # pragma: no cover - interface
        """Start (idempotent). Returns {"url": ..., "token": ...} to point the client at."""
        raise NotImplementedError


_provider = None
# Ten panels may notice a stopped server at the same moment (one per open tool).
# The server's `up` is idempotent, but two launches racing each other are not
# something to rely on: they take turns, and the second finds it running.
_start_lock = threading.Lock()


def register_provider(provider) -> None:
    """Called once by SlicerCloud at startup. `None` unregisters (tests)."""
    global _provider
    _provider = provider


def provider():
    return _provider


def start_serialized(local: LocalServer) -> dict:
    with _start_lock:
        return local.start()
