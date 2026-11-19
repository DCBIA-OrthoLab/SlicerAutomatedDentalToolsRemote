"""What the tool panels may ask of the local server: is it installed, start it.

**This module imports neither `slicer` nor `qt`.** The panels live in
ServerToolsCore and must not import SlicerCloud; at startup SlicerCloud hands
them a `Provider` (see `ServerToolsCoreLib.gate`). A panel asks for a snapshot
on the main thread, then calls `installed()` and `start()` from a worker.

Starting is serialised with the SlicerCloud panel's own jobs (`gate`) and goes
through `deployment.ensure_up`, which waits for a server that is starting instead
of launching it again. It is also idempotent on the server's side (`up` over our own running server
reports `already_running` and touches nothing), so a start from here is safe
to repeat. Two things it never does: choose a port or an address (the install
remembers both, and 'Try another port' must survive), and change which server
the modules use. Pointing the modules is the caller's decision.
"""

from ServerToolsCoreLib import gate

from . import state
from .backends import DeploymentError

# How long a tool panel waits for an install, an update or a start running in the
# SlicerCloud panel before telling the person: an install can be long.
_WAIT_FOR_PANEL_SECONDS = 3600.0


class LocalServer:
    """A deployment, seen as the tool panels need it."""

    def __init__(self, deployment, wait_seconds: float = _WAIT_FOR_PANEL_SECONDS):
        self._deployment = deployment
        self._wait_seconds = wait_seconds

    def installed(self) -> bool:
        """Everything the server needs is on disk (the same test the panel's
        state uses), so a half-finished install is not started by accident."""
        try:
            status = self._deployment.status()
        except DeploymentError:
            return False
        return state.is_installed(status, self._deployment.backend)

    def start(self) -> dict:
        """Start it, or find it running. Raises `DeploymentError` with the
        sentence to show. The result carries what a panel needs to point the
        modules at it: `url` (None when the server publishes none usable, with
        `no_url_message` saying why), `token` and `already_running`."""
        deployment = self._deployment
        # Never while the SlicerCloud panel is installing, starting or updating:
        # two launches racing each other is how a server found its own port taken.
        if not gate.wait_for_idle(self._wait_seconds):
            raise DeploymentError("VISOR Local is being set up from the Slicer Cloud panel. "
                                  "Open it to follow the progress, then come back.")
        # And never a second launch over a server that is running or starting:
        # `ensure_up` reports the first, waits for the second, launches otherwise.
        up = deployment.ensure_up(port=None, bind=None)
        status = deployment.status()
        backend = deployment.backend
        url = backend.client_url(status)
        token = up.get("token") or deployment.token()
        return {
            "url": url,
            "token": token,
            "already_running": bool(up.get("already_running")),
            "no_url_message": None if url else backend.no_url_message(),
        }


class Provider:
    """Registered with ServerToolsCoreLib.gate by the SlicerCloud module."""

    def __init__(self, deployment_factory):
        self._factory = deployment_factory

    def snapshot(self) -> LocalServer:
        """Main thread: reads the settings the factory reads."""
        return LocalServer(self._factory())
