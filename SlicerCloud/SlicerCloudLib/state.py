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


def configured_remote(client_url) -> bool:
    """The client is set to a server that is NOT on this machine: the one thing
    that must not be replaced without asking. Unset (the `.invalid` placeholder),
    empty, malformed and loopback addresses are not."""
    if not client_url:
        return False
    try:
        host = (urlsplit(str(client_url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host or host == "invalid" or host.endswith(".invalid"):
        return False
    return host not in tls.LOCAL_PIN_HOSTS and host != "localhost"


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
        shown |= {INSTALL, REFRESH, LOGS, UPDATE}
    elif kind == State.STARTING:
        shown |= {STOP, REFRESH, LOGS}
    elif kind == State.RUNNING:
        shown |= {STOP, REFRESH, LOGS, UPDATE}
        if flags.client != CLIENT_HERE:
            primary = USE_HERE
            shown.add(USE_HERE)
        else:
            primary = STOP
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


def update_action(state: ServerState) -> str:
    """What the one Update button does: "update" when something newer is known
    (or the clone sits on another branch than the one asked), otherwise "check",
    which fetches the remote to find out. 'Check for updates' used to be a second
    control beside it."""
    return "update" if state.flags.update_available else "check"


def update_label(state: ServerState, backend) -> str:
    texts = backend.texts
    return texts.update_label if update_action(state) == "update" else texts.check_label


Layout = collections.namedtuple("Layout", "grid steps folder stop_on_exit log")


def layout(state: ServerState, backend, developer: bool) -> Layout:
    """Which blocks of the panel `state` shows.

    `grid`       "all" the raw status rows, "prerequisites" the few checks a person
                 acts on, "none" nothing (a sentence says it instead)
    `steps`      the list of install steps with the global progress bar
    `folder`     "edit" a path field (developer mode), "change" the folder stated
                 with a Change button (before the first install: it is fixed once
                 it has started), "stated" a line of text
    `stop_on_exit` the 'stop when Slicer closes' line, wherever a server exists
    `log`        the log pane as a block of the panel (developer mode, or a backend
                 that is not guided)
    """
    guided = backend.guided
    kind = state.kind
    before_install = kind in (State.NEEDS_SETUP, State.NOT_INSTALLED)
    if developer or not guided:
        grid = "all"
    elif kind == State.NEEDS_SETUP:
        grid = "prerequisites"
    else:
        grid = "none"
    if developer:
        folder = "edit"
    elif before_install:
        folder = "change"
    else:
        folder = "stated"
    if guided:
        stop_on_exit = kind in (State.INSTALLED_STOPPED, State.STARTING, State.RUNNING,
                                State.PORT_TAKEN)
    else:
        stop_on_exit = True
    return Layout(grid=grid, steps=guided and kind == State.INSTALLING, folder=folder,
                  stop_on_exit=stop_on_exit, log=developer or not guided)
