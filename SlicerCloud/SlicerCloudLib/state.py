"""What state the local server is in, and what the panel may offer for it.

**This module imports neither `slicer` nor `qt`.** `classify` is a pure
function of the last `status` dict (what `server_ctl.py status` reported, or the
host probe before a clone exists), a few facts about this machine, the running
job and the address the client is pointed at. The panel used to have no notion
of state at all: it painted a grid and every button was always there, so "Start
server" stayed on screen while the server was running. Now the state decides the
buttons, and a test can say so without a widget.

Two axes, kept apart because they change for different reasons:

* the server on this machine: `State`, from the prerequisites, the install and
  the process;
* the client's connection, which is not a property of the server: `Flags`
  carries `update_available`, `branch_mismatch` and `client` (does the client
  point at this server, at another one, or at none).

    NEEDS_SETUP        a prerequisite is missing (Docker, git, uv...)
    NOT_INSTALLED      nothing, or not everything, is downloaded yet
    INSTALLING         an install or a start job is running
    INSTALLED_STOPPED  installed, no process
    STARTING           a process, /health not answering yet
    RUNNING            /health answers
    PORT_TAKEN         installed, stopped, and a foreign program holds the port
    BROKEN             a folder is there that cannot be driven
"""

import collections
from urllib.parse import urlsplit

from ServerToolsCoreLib import tls


class State:
    NEEDS_SETUP = "needs_setup"
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    INSTALLED_STOPPED = "installed_stopped"
    STARTING = "starting"
    RUNNING = "running"
    PORT_TAKEN = "port_taken"
    BROKEN = "broken"


#: Where the client's address points, relative to the server this panel manages.
CLIENT_HERE = "here"
CLIENT_ELSEWHERE = "elsewhere"
CLIENT_UNSET = "unset"

Flags = collections.namedtuple("Flags", "update_available branch_mismatch client busy")

ServerState = collections.namedtuple("ServerState", "kind flags missing")


def _port_of(url):
    try:
        return urlsplit(str(url or "")).port
    except ValueError:
        return None


def client_target(status, client_url, backend=None) -> str:
    """Whether the address every module uses is THIS server, another one, or none.

    "Here" is a loopback address on the port this server serves, or the
    deployment's own public address. The placeholder (`.invalid`) is "unset".
    """
    if not client_url:
        return CLIENT_UNSET
    try:
        parts = urlsplit(str(client_url))
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return CLIENT_ELSEWHERE
    if host == "invalid" or host.endswith(".invalid"):
        return CLIENT_UNSET
    server = (status or {}).get("server") or {}
    public = (server.get("public_url") or (status or {}).get("public_url") or "").rstrip("/")
    if public and str(client_url).rstrip("/") == public:
        return CLIENT_HERE
    if host in tls.LOCAL_PIN_HOSTS or host in ("localhost",):
        served = _port_of(server.get("url") or (status or {}).get("url"))
        if served is not None and port == served:
            return CLIENT_HERE
    return CLIENT_ELSEWHERE


def is_installed(status, backend) -> bool:
    """Everything the server needs is on disk. Natively that is the server, the
    tools' checkout AND every tool environment: a half-finished install is "not
    installed" and the same button finishes it, each step being idempotent."""
    status = status or {}
    if not status.get("cloned"):
        return False
    if not backend.has_tools_checkout:
        return True
    tools = status.get("tools") or {}
    if not tools.get("present"):
        return False
    venvs = tools.get("venvs") or {}
    return bool(venvs) and all(venvs.values())


def classify(status, backend, facts=None, job=None, client_url=None) -> ServerState:
    """The state, with the flags that are not states.

    `status`  the dict of `LocalServerDeployment.status()`; None before the first
              answer (reads as NOT_INSTALLED with nothing said yet)
    `facts`   what only this machine can say: `disk_free`, `port`, `port_in_use`,
              `apple_silicon` (see backends.Backend.checks)
    `job`     "install" while an install/start job runs, any other string for
              another job, None for none
    """
    status = status or {}
    facts = dict(facts or {})
    clone = status.get("clone") or {}
    container = status.get("container") or {}
    running = bool(container.get("running"))
    healthy = bool((status.get("server") or {}).get("healthy"))
    facts["server_running"] = running

    flags = Flags(
        update_available=bool(clone.get("behind")) or bool(clone.get("branch_mismatch")),
        branch_mismatch=bool(clone.get("branch_mismatch")),
        client=client_target(status, client_url, backend),
        busy=job,
    )

    if job == "install":
        return ServerState(State.INSTALLING, flags, ())

    # A server that is up needs no prerequisite: whatever is "missing" is moot.
    if running:
        return ServerState(State.RUNNING if healthy else State.STARTING, flags, ())

    missing = tuple(check for check in backend.checks(status, facts) if check.blocking and not check.ok)
    if missing:
        return ServerState(State.NEEDS_SETUP, flags, missing)

    if not is_installed(status, backend):
        return ServerState(State.NOT_INSTALLED, flags, ())
    if status.get("cloned") and not clone.get("is_git_repo"):
        return ServerState(State.BROKEN, flags, ())
    if facts.get("port_in_use"):
        return ServerState(State.PORT_TAKEN, flags, ())
    return ServerState(State.INSTALLED_STOPPED, flags, ())


# -- what each state offers --------------------------------------------------

#: The actions the panel has widgets for. A state lists the ones it SHOWS; every
#: other widget is hidden, not greyed.
INSTALL = "install"        # install, start, or fix: one button whose label follows the state
UPDATE = "update"
STOP = "stop"
USE_HERE = "use_here"      # point every module at this server
TRY_PORT = "try_port"
SWITCH_BACK = "switch_back"
LOGS = "logs"
REFRESH = "refresh"
GPU = "gpu"
CANCEL = "cancel"

Actions = collections.namedtuple("Actions", "primary shown")


def actions(state: ServerState, gpu_fixable: bool = False, can_switch_back: bool = False) -> Actions:
    """The primary action and the set of widgets to show for `state`.

    `gpu_fixable`: a card is present that Docker cannot reach (the link is shown
    only then, in any state where the server is not being worked on).
    `can_switch_back`: a previous server address was kept when this one replaced it.
    """
    kind, flags = state.kind, state.flags
    shown = set()
    primary = None

    if kind == State.INSTALLING:
        return Actions(None, frozenset({CANCEL}))

    if kind == State.NEEDS_SETUP:
        primary = INSTALL
        shown |= {INSTALL, REFRESH}
    elif kind == State.NOT_INSTALLED:
        primary = INSTALL
        shown |= {INSTALL, REFRESH}
    elif kind == State.BROKEN:
        shown |= {REFRESH, LOGS}
    elif kind == State.PORT_TAKEN:
        primary = TRY_PORT
        shown |= {TRY_PORT, REFRESH, LOGS}
    elif kind == State.INSTALLED_STOPPED:
        primary = INSTALL
        shown |= {INSTALL, REFRESH, LOGS}
        if flags.update_available:
            shown.add(UPDATE)
    elif kind == State.STARTING:
        shown |= {STOP, REFRESH, LOGS}
    elif kind == State.RUNNING:
        shown |= {STOP, REFRESH, LOGS}
        if flags.client != CLIENT_HERE:
            primary = USE_HERE
            shown.add(USE_HERE)
        else:
            primary = STOP
        if flags.update_available:
            shown.add(UPDATE)
    if gpu_fixable and kind in (State.NEEDS_SETUP, State.NOT_INSTALLED, State.INSTALLED_STOPPED,
                                State.RUNNING, State.STARTING):
        shown.add(GPU)
    if can_switch_back and flags.client == CLIENT_HERE:
        shown.add(SWITCH_BACK)
    return Actions(primary, frozenset(shown))


def install_label(state: ServerState, backend) -> str:
    """The text of the one install/start/fix button for `state`."""
    texts = backend.texts
    if state.kind == State.NEEDS_SETUP:
        remedy = next((check.remedy for check in state.missing if check.remedy), None)
        return f"Fix: {remedy.label}" if remedy else texts.install_label
    if state.kind == State.INSTALLED_STOPPED:
        return texts.start_label
    return texts.install_label
