"""The two ways to run the tool server on this machine: Docker, or a plain process.

**This module imports neither `slicer` nor `qt`** (same dependency rule as
`deploy.py`, see ARCHITECTURE.md), so everything here is unit-tested in plain CI.

Why an object and not a flag. The two backends differ by *sequences* (install is
`clone, status, up` for Docker and `check, clone, clone tools, build the tool
environments, up` natively), by prerequisites (docker, compose, a group, a GPU
runtime, against git, uv and Apple silicon), by remedies, by which address the
client may be given, by which network choices exist, and by how a failure is
explained. Thirty `if native` spread over two files said so, and each bug found
in the audit (B2, I2, I3) was one of them forgotten. A backend is chosen ONCE,
from the platform (`deploy.select_backend`), and every one of those decisions is
then a method call:

    subprocess_env     the environment of every server_ctl.py it starts
    common_args        the flags every subcommand carries (`--native --tools-dir D`)
    start_args         the flags `up` and `update` add (the pinned certificate)
    update_args        the flags `update` adds (the tools branch)
    checks             the rows of the status grid, each with its remedy
    install_plan       the idempotent steps behind the one install/start button
    client_url         the address the client may be given, or None
    hint               the sentence naming the single next thing to do
    texts              the words that differ ("container" or "server", tooltips)

`LocalServerDeployment` keeps what is the same for both: running a process,
streaming its log, cancelling, git. The backend never spawns anything itself; it
asks the deployment it is given.
"""

import collections
import json
import os
import shutil
from urllib.parse import urlsplit

from ServerToolsCoreLib import tls

# -- shared constants (re-exported by deploy.py, which is where callers look) ---

DEFAULT_TOOLS_REPO_URL = "https://github.com/DCBIA-OrthoLab/SADT-VISOR.git"
DEFAULT_TOOLS_BRANCH = "main"
DEFAULT_SERVER_URL = "https://localhost:8000"

#: What `bind` means when nothing has been chosen. Loopback, because the
#: Docker listener's certificate is self-signed (no other machine can verify
#: it) and the server carries medical images. A native server is loopback only.
BIND_LOCALHOST = "127.0.0.1"
#: The empty string is not "unset" here: it is how the compose file is told to
#: publish on every interface, IPv4 and IPv6 both. See its own comment.
BIND_EVERYWHERE = ""

NATIVE_SCRIPT = os.path.join("scripts", "server_ctl_native.py")
#: Where the branch asked for is fetched to be looked at, so the clone's own
#: branches, its working tree and its remote are never touched by a check.
PROBE_REF = "refs/slicercloud/probe"

NO_LOCAL_TLS_MESSAGE = (
    "The server on this Mac is running, but it did not report that it serves HTTPS with the "
    "certificate SlicerCloud made for it, so nothing was saved: the extension only talks https. "
    "Update the server code (the Update server button), then press Refresh."
)

NO_PUBLIC_URL_MESSAGE = (
    "This server has no public address yet. The extension only talks to a server "
    "through an https:// address whose certificate a public authority signed -- "
    "for a server on this computer too. Give it one, then press Refresh:\n\n"
    "  - set `tailscale serve` up by hand on the server's machine, with the tailscale "
    "command (it publishes the server as https://<name>.<tailnet>.ts.net), or\n"
    "  - set PUBLIC_URL in the server's .env to its https:// address behind a "
    "reverse proxy with a Let's Encrypt or institutional certificate, or served "
    "directly with its own public certificate."
)

GIT_MISSING_MESSAGE = (
    "git is not installed. Install it and restart Slicer.\n"
    "  Debian/Ubuntu: sudo apt-get install -y git\n"
    "  macOS:         xcode-select --install\n"
    "  Windows:       https://git-scm.com/download/win"
)

#: A rough figure, said as "about": the tool environments of the native backend
#: are measured at several hundred MB each, 23 tools. Only ever shown, never
#: used to refuse anything.
NATIVE_SETUP_BYTES = 15 * 1024 ** 3


class DeploymentError(Exception):
    """Something the user can act on, with a message written for them."""


class BranchSwitch(collections.namedtuple("BranchSwitch", "what path current wanted dirty")):
    """A managed clone that is on another branch than the form asks for."""


def emit(progress_cb, message: str) -> None:
    if progress_cb and message:
        progress_cb(message)


def human_size(size) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# -- what a backend hands the panel -----------------------------------------

#: One row of the status grid. `ok` colours it, `remedy` (a `Remedy` or None) is
#: what can be done about it, `blocking` says whether the server cannot be
#: started while it is not `ok`.
Check = collections.namedtuple("Check", "key label ok detail remedy blocking")

#: `key` names what the panel does about it ("docker_install", "docker_group",
#: "gpu", "git", "uv"); `label` is the button's text.
Remedy = collections.namedtuple("Remedy", "key label")

#: One idempotent step of the install/start plan. `weight` is for a global bar.
#: `run(deployment, progress_cb, options)`; the last step returns the result of `up`.
Step = collections.namedtuple("Step", "key label weight run")


# -- the address the client may use ---------------------------------------

def public_client_url(result):
    """The deployment's PUBLIC https address, or None.

    server_ctl.py reports it as `public_url` (top level from `up`, under
    `server` from `status`): PUBLIC_URL from the deployment's .env when set,
    otherwise the address `tailscale serve` publishes, otherwise "". Either is
    an https:// address with a publicly-signed certificate. The Docker
    listener's own `url` (self-signed, which the client refuses) is never
    handed over, and neither is a loopback `public_url`.
    """
    result = result or {}
    server = result.get("server") or {}
    for candidate in (result.get("public_url"), server.get("public_url")):
        if not candidate:
            continue
        try:
            url = tls.require_https(candidate)
        except ValueError:
            continue
        if not tls.is_loopback(urlsplit(url).hostname):
            return url
    return None


def local_client_url(result):
    """`https://127.0.0.1:<port>` for a native server serving the pinned
    certificate, or None.

    Handed over only when ALL of these hold, so that no result a server (or a
    stale clone) produces can send the client to an address it cannot verify:

    - the result reports `tls.enabled`, which is the server saying it was
      started with the certificate this client made (contract in
      ARCHITECTURE.md, "Native server on a Mac");
    - the server's own URL is on this machine (127.0.0.1 or ::1) and
      carries a port; only that port is used, host and scheme are ours;
    - the pinned certificate exists and passes `tls.pinned_certificate`, i.e.
      the client WILL verify against it rather than fall back to the public
      authorities, which a self-signed certificate cannot satisfy;
    - if the result names the certificate it serves, it is that same file.
    """
    result = result or {}
    server = result.get("server") or {}
    info = result.get("tls") or server.get("tls") or {}
    if not (isinstance(info, dict) and info.get("enabled")):
        return None
    pin = tls.pinned_certificate("127.0.0.1")
    if not pin:
        return None
    reported = info.get("cert")
    if reported and os.path.realpath(str(reported)) != os.path.realpath(pin):
        return None
    for candidate in (result.get("url"), server.get("url")):
        try:
            parts = urlsplit(str(candidate or ""))
            host, port = parts.hostname, parts.port
        except ValueError:
            continue
        if port and (host or "").lower() in tls.LOCAL_PIN_HOSTS:
            return f"https://127.0.0.1:{port}"
    return None


# -- the sentences a state is described with --------------------------------
#
# `kind` is a `state.State` value, passed as its string so that this module need
# not import `state`. Every method returns plain English and never mentions the
# other backend.

class Texts:
    """The words that differ between backends. Plain attributes, no behaviour."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class Backend:
    """Base class: the parts that are the same, and the interface."""

    name = ""
    native = False
    #: The one `bind` this backend can use, or None when the user may choose.
    fixed_bind = None
    #: Whether `server_ctl.py` is given a folder of tools to build environments from.
    has_tools_checkout = False
    #: The four-screen presentation (checks, one sentence, steps, ready) instead
    #: of the full grid; Docker keeps the grid it always had.
    guided = False
    #: The keys of `checks()` a person is shown before setting up.
    prerequisites = ()
    texts = None

    # -- environment and arguments -------------------------------------

    def subprocess_env(self, base) -> dict:
        """`base` plus the backend NAMED to server_ctl.py, whose own default on a
        Mac (native) would otherwise contradict an Intel Mac's choice (Docker)."""
        env = dict(base)
        env["VISOR_BACKEND"] = self.name
        return env

    def common_args(self, deployment, bind=None) -> list:
        return []

    def start_args(self, deployment, progress_cb=None) -> list:
        return []

    def update_args(self, deployment, progress_cb=None) -> list:
        return []

    def explain_update_error(self, message: str, asked_tools_branch: bool) -> str:
        return message

    def explain_failure(self, deployment, stderr_lines):
        """A better message than the raw one for a failed server_ctl.py, or None."""
        return None

    # -- the host before a clone exists --------------------------------

    def probe(self, host) -> dict:
        raise NotImplementedError

    # -- operations that only one backend has --------------------------

    def require_support(self, deployment, progress_cb=None) -> None:
        """Refuse, before anything is modified, a branch that cannot run here."""

    def before_update(self, deployment, progress_cb=None) -> None:
        """What an update needs done first, before anything is stopped."""

    def managed_repositories(self, deployment) -> list:
        """`(what, path, wanted branch, present)` for each clone this backend manages."""
        return [("server repository", deployment.install_dir, deployment.branch, deployment.is_cloned)]

    def clone_tools(self, deployment, progress_cb=None) -> None:
        return None

    def install_tools(self, deployment, progress_cb=None, tools=None, force=False) -> dict:
        raise DeploymentError("install-tools belongs to the native backend.")

    def after_clone(self, deployment) -> None:
        return None

    # -- what the panel reads ------------------------------------------

    def checks(self, status, facts=None) -> list:
        raise NotImplementedError

    def install_plan(self, status) -> list:
        raise NotImplementedError

    def client_url(self, result):
        return public_client_url(result)

    def no_url_message(self) -> str:
        return NO_PUBLIC_URL_MESSAGE

    def hint(self, kind: str, status, facts=None, developer: bool = False) -> str:
        raise NotImplementedError

    # -- pieces shared by both backends' grids ---------------------------

    @staticmethod
    def _git_check(status):
        git = status.get("git") or {}
        ok = bool(git.get("available"))
        return ok, (git.get("version") or "not installed")

    @staticmethod
    def _clone_check(status):
        clone = status.get("clone") or {}
        if not status.get("cloned"):
            return False, f"not installed in {status.get('repo_root', '')}"
        if not clone.get("is_git_repo"):
            return False, clone.get("error") or "not a git clone"
        if clone.get("branch_mismatch"):
            return False, (f"on '{clone.get('branch')}', but this deployment asks for "
                           f"'{clone.get('configured_branch')}': press 'Update server' to switch it")
        behind = clone.get("behind") or 0
        text = f"{clone.get('branch')} at {clone.get('commit')}, "
        text += f"{behind} commit(s) behind, press Update" if behind else "up to date"
        if not clone.get("checked_remote"):
            text += " (as of the last fetch)"
        if clone.get("dirty"):
            text += " [local changes]"
        return not behind, text

    @staticmethod
    def _health_check(status):
        healthy = bool((status.get("server") or {}).get("healthy"))
        url = (status.get("server") or {}).get("url") or DEFAULT_SERVER_URL
        return healthy, f"{url}: {'answering' if healthy else 'no answer'}"

    @staticmethod
    def _port_check(facts):
        facts = facts or {}
        if facts.get("port_in_use") is None:
            return None
        number = facts.get("port") or 8000
        if facts.get("server_running"):
            return True, f"{number}: in use by the server itself"
        if facts.get("port_in_use"):
            return False, f"{number}: another program is already using it"
        return True, f"{number}: free"


class DockerBackend(Backend):
    """The server in a container: Linux, Windows, and an Intel Mac."""

    name = "docker"
    native = False
    fixed_bind = None
    has_tools_checkout = False

    texts = Texts(
        title="Local server",
        unit="container",
        install_label="Install and start",
        start_label="Start server",
        update_label="Update server",
        check_label="Check for updates",
        update_found="A new version of the server is available.",
        up_to_date="The server is up to date.",
        stop_on_exit_label="Stop the server when Slicer closes",
        install_tooltip=("Clone the server repository if needed, then start its container and wait "
                         "for it to answer. Safe to run again: an existing install is reused and "
                         "its API key kept."),
        update_tooltip=("Fetch the remote, fast-forward the clone if it has fallen behind, and "
                        "relaunch the container. Does nothing when the clone is current and the "
                        "server is answering."),
        stop_on_exit_tooltip=(
            "Leaves nothing running in the background once you are done. Idle, the container "
            "costs about 220 MB of memory and no CPU or GPU, but after a run it keeps the "
            "loaded models resident until it is restarted.\n\n"
            "Restarting is cheap: the container is stopped, not deleted, so its dependencies "
            "stay installed and a later start takes a few seconds.\n\n"
            "Turn this OFF if this machine serves other people: closing Slicer would stop "
            "their server too."),
        bind_tooltip=(
            "Which machines can reach this server. 'This computer only' is the default and "
            "keeps it off the network entirely.\n\n"
            "This client only talks https, and only to a certificate it can verify: a "
            "publicly-signed one, or, for a server on this very Mac, the certificate "
            "this panel made for it. To share a Docker server, give it a public address "
            "with a publicly-signed certificate: `tailscale serve`, or a reverse proxy "
            "or the server itself with a Let's Encrypt or institutional certificate."),
        use_label="Use this server",
        folder_note="Where the server repository and its model bundles (up to ~29 GB) are kept.",
    )

    def probe(self, host) -> dict:
        """The prerequisites, answered without a clone (see deploy.probe_host)."""
        git_version, _ = host.version(["git", "--version"]) if host.which("git") else (None, None)
        docker_version, _ = (host.version(["docker", "--version"])
                             if host.which("docker") else (None, None))
        daemon, daemon_error = (None, "docker is not installed")
        if docker_version:
            daemon, daemon_error = host.version(["docker", "info", "--format", "{{.ServerVersion}}"])
        compose_version, _ = host.version(["docker", "compose", "version"]) if docker_version else (None, None)

        # Both mechanisms, exactly as scripts/server_ctl.py's gpu_info() decides it
        # (an "nvidia" runtime is no longer the only answer). Duplicated here
        # because this runs BEFORE the clone exists, so there is no server_ctl.py
        # to ask.
        runtime, cdi = False, False
        if daemon:
            runtimes, _err = host.version(["docker", "info", "--format", "{{json .Runtimes}}"])
            runtime = "nvidia" in (runtimes or "")
            # `.DiscoveredDevices` does not exist before docker 28, where an unknown
            # field fails the whole template. A `None` here means "no CDI", never
            # "could not check".
            devices, _err = host.version(["docker", "info", "--format", "{{json .DiscoveredDevices}}"])
            cdi = _has_nvidia_cdi_device(devices)

        return {
            "native": False,
            "git": {"available": bool(git_version), "version": git_version},
            "docker": {
                "available": bool(docker_version),
                "version": docker_version,
                "daemon": bool(daemon),
                "error": None if daemon else daemon_error,
                # The same flag scripts/server_ctl.py reports: the panel answers
                # "you are not in the docker group" with instructions and "the
                # daemon is down" with an installer, and must not match English text.
                "needs_group": bool(
                    not daemon and "permission denied" in (daemon_error or "").lower()),
            },
            "compose": {"available": bool(compose_version), "version": compose_version},
            "gpu": {
                "nvidia_runtime": runtime or cdi,
                "nvidia_smi": bool(host.which("nvidia-smi")),
                "gpu_access": "runtime" if runtime else ("cdi" if cdi else None),
            },
        }

    # -- the grid ---------------------------------------------------------

    def checks(self, status, facts=None) -> list:
        git_ok, git_text = self._git_check(status)
        docker = status.get("docker") or {}
        if docker.get("daemon"):
            docker_ok, docker_text, docker_remedy = True, docker.get("version") or "running", None
        elif docker.get("needs_group"):
            # Deliberately short: a cell in a status grid, and the full remedy is
            # six numbered steps. The dialog carries those.
            docker_ok, docker_text = False, "installed, but this account may not use it yet"
            docker_remedy = Remedy("docker_group", "Show the steps")
        elif docker.get("available"):
            docker_ok, docker_text = False, f"installed, but: {docker.get('error') or '?'}"
            docker_remedy = Remedy("docker_install", "Install Docker")
        else:
            docker_ok, docker_text = False, "not installed"
            docker_remedy = Remedy("docker_install", "Install Docker")

        compose = status.get("compose") or {}
        gpu = status.get("gpu") or {}
        # `gpu_access` names WHICH mechanism answered: an nvidia runtime, or a CDI
        # device. Absent when the clone predates it, which is why the text falls
        # back to the runtime wording rather than to "unknown".
        gpu_remedy = None
        if gpu.get("nvidia_runtime"):
            gpu_ok = True
            gpu_text = ("reachable through CDI: the GPU service will be used"
                        if gpu.get("gpu_access") == "cdi"
                        else "nvidia runtime available: the GPU service will be used")
        elif gpu.get("nvidia_smi"):
            gpu_ok, gpu_text = False, "a card is present but docker cannot reach it, running on CPU"
            gpu_remedy = Remedy("gpu", "Enable GPU support")
        else:
            gpu_ok, gpu_text = False, "no GPU, running on CPU (everything works, slowly)"

        clone_ok, clone_text = self._clone_check(status)
        container = status.get("container") or {}
        health_ok, health_text = self._health_check(status)
        return [
            Check("git", "Git", git_ok, git_text, None if git_ok else Remedy("git", "Install git"), True),
            Check("docker", "Docker", docker_ok, docker_text, docker_remedy, True),
            Check("compose", "Docker Compose", bool(compose.get("available")),
                  compose.get("version") or "not installed",
                  None if compose.get("available") or not docker.get("available")
                  else Remedy("docker_install", "Install Docker"), True),
            Check("gpu", "GPU", gpu_ok, gpu_text, gpu_remedy, False),
            Check("clone", "Server repository", clone_ok, clone_text, None, False),
            Check("container", "Container", bool(container.get("running")),
                  f"{status.get('service', '?')}: {container.get('state') or 'not created yet'}",
                  None, False),
            Check("health", "Server", health_ok, health_text, None, False),
        ]

    # -- install / start --------------------------------------------------

    def install_plan(self, status) -> list:
        def clone(deployment, progress_cb, _options):
            deployment.clone(progress_cb=progress_cb)

        def check_docker(deployment, progress_cb, _options):
            current = deployment.status(progress_cb=progress_cb)
            if not (current.get("docker") or {}).get("daemon"):
                raise DeploymentError(
                    "Docker is not usable yet: {0}\n\n"
                    "Install it by running this in a terminal, then log out and back in:\n\n"
                    "    sudo sh {1}".format(
                        (current.get("docker") or {}).get("error") or "the daemon did not answer",
                        deployment.install_docker_script))

        def start(deployment, progress_cb, options):
            return deployment.up(progress_cb=progress_cb, port=options.get("port"),
                                 bind=options.get("bind"))

        return [Step("clone", "Download the server", 1, clone),
                Step("docker", "Check Docker", 1, check_docker),
                Step("start", "Start the container", 3, start)]

    # -- sentences ----------------------------------------------------------

    def hint(self, kind: str, status, facts=None, developer: bool = False) -> str:
        status = status or {}
        docker = status.get("docker") or {}
        clone = status.get("clone") or {}
        if kind == "needs_setup":
            if not docker.get("available"):
                return ("Docker is missing. Press 'Fix: Install Docker': it will tell you the exact "
                        "command to run in a terminal.")
            if docker.get("needs_group"):
                return ("Docker is installed, but this account may not use it yet. Press "
                        "'Fix: Show the steps': it shows the one command to run, step by step.")
            if not docker.get("daemon"):
                return f"Docker is installed but not usable by this account: {docker.get('error') or ''}"
            return "A tool this server needs is missing. See the list above."
        if kind == "not_installed":
            return ("Nothing installed yet. Press 'Install and start' to clone the server and "
                    "bring it up.")
        if kind == "installing":
            return "Installing. You can keep using Slicer meanwhile."
        if kind == "broken":
            return "The install in this folder cannot be used. Check the log, or choose another folder."
        if kind == "port_taken":
            return ("Another program is using the port this server needs. Press 'Try another port', "
                    "or stop that program.")
        if kind in ("installed_stopped", "starting", "running"):
            if clone.get("branch_mismatch"):
                return (f"The clone is on branch '{clone.get('branch')}' but this deployment asks for "
                        f"'{clone.get('configured_branch')}'. Press 'Update server' to switch it.")
            if clone.get("behind"):
                return "A newer version of the server is available. Press 'Update server'."
        if kind == "installed_stopped":
            return "The server is installed but not running. Press 'Start server'."
        if kind == "starting":
            return ("The container is running but not answering yet; it may still be installing "
                    "dependencies. Check the Log.")
        # Last, and only once everything else is right: the server WORKS on the
        # CPU, so this must never displace a step that is actually blocking.
        gpu = status.get("gpu") or {}
        if gpu.get("nvidia_smi") and not gpu.get("nvidia_runtime"):
            return ("The server is up, but on the CPU: this machine has a GPU that Docker "
                    "cannot reach. Press 'Enable GPU support' to set that up.")
        return "The server is up. Pick the tools you need under 'Manage downloads'."


class NativeBackend(Backend):
    """The server as a plain process on this Mac (Apple silicon): no Docker, the
    GPU is the machine's own, and the client is given the pinned local address."""

    name = "native"
    native = True
    fixed_bind = BIND_LOCALHOST
    has_tools_checkout = True
    guided = True
    prerequisites = ("git", "uv", "gpu", "disk", "port")

    texts = Texts(
        title="VISOR Local",
        unit="server",
        install_label="Set up VISOR Local",
        start_label="Start VISOR Local",
        update_label="Update VISOR Local",
        check_label="Check for updates",
        update_found="A new version is available.",
        up_to_date="VISOR Local is up to date.",
        stop_on_exit_label="Stop VISOR Local when Slicer closes (it starts again when you open a tool)",
        install_tooltip=("Download the server and the tools, prepare the tools' Python "
                         "environments (slow once, instant after), then start the server on this "
                         "Mac and wait for it to answer. Safe to run again: each step is skipped "
                         "when it is already done."),
        update_tooltip=("Fetch the server and the tools, rebuild what changed, and relaunch the "
                        "server if it is running. Does nothing when everything is current."),
        stop_on_exit_tooltip=(
            "Leaves nothing running in the background once you are done. The server is a plain "
            "process: while idle it costs some memory and no GPU, but after a run it keeps the "
            "loaded models resident until it is restarted.\n\n"
            "It starts again by itself when you open a tool.\n\n"
            "Turn this OFF if other people use this Mac's server: closing Slicer would stop it "
            "for them too."),
        bind_tooltip=("The server on this Mac listens on this computer only. To share it, run it "
                      "behind a reverse proxy or tailscale serve with a publicly-signed certificate."),
        use_label="Use VISOR Local",
        folder_note=("Where the server, the tools and their Python environments (about 15 GB) "
                     "are kept."),
    )

    # -- environment and arguments -------------------------------------

    def common_args(self, deployment, bind=None) -> list:
        """The flags that make server_ctl.py use its native backend. A native
        server listens on this computer only, so a wider `bind` is refused here,
        before anything starts, rather than producing a server whose
        self-signed certificate nobody else can verify."""
        if bind not in (None, BIND_LOCALHOST):
            raise DeploymentError(
                "The server on this Mac listens on this computer only. To share it, run it "
                "behind a reverse proxy or tailscale serve with a publicly-signed certificate."
            )
        return ["--native", "--tools-dir", deployment.tools_dir]

    def start_args(self, deployment, progress_cb=None) -> list:
        """`--tls-cert`/`--tls-key` for a native `up` or `update`. The only place
        the key's path is given to anything: the server reads it, the client
        never does. The contract with server_ctl.py is in ARCHITECTURE.md."""
        pair = deployment.ensure_certificate(progress_cb)
        return ["--tls-cert", pair["cert"], "--tls-key", pair["key"]]

    def update_args(self, deployment, progress_cb=None) -> list:
        """`--tools-branch B [--tools-url U]` for a native `update`.

        `update --native` puts the tools checkout on the branch asked for, so
        the Tools branch field means something after the first clone, the way
        the Branch field does. The URL is only sent when it is not the default
        one: the server then fetches from `origin` and never has to compare.

        A server installed before those options existed would die on an
        argparse error. So this asks it (`update --help`) first. Only a
        non-default choice is an error there: sending nothing leaves the tools
        where they are, which is all an older server could ever do, but
        dropping a branch the user typed would be a silent lie.
        """
        wanted = ["--tools-branch", deployment.tools_branch]
        if deployment.tools_repo_url != DEFAULT_TOOLS_REPO_URL:
            wanted += ["--tools-url", deployment.tools_repo_url]
        if deployment._ctl_publishes("--tools-branch"):
            return wanted
        if (deployment.tools_branch == DEFAULT_TOOLS_BRANCH
                and deployment.tools_repo_url == DEFAULT_TOOLS_REPO_URL):
            emit(progress_cb, "This server cannot switch the tools branch (it is older than that option); "
                              "leaving the tools checkout as it is.")
            return []
        raise DeploymentError(
            f"This server is too old to switch the tools to branch '{deployment.tools_branch}'"
            f"{'' if deployment.tools_repo_url == DEFAULT_TOOLS_REPO_URL else ' of ' + deployment.tools_repo_url}: "
            f"its server_ctl.py has no --tools-branch option. The tools were not switched and the running "
            f"server was not touched. Update the server code first (Branch field, then Update server "
            f"with the Tools branch set back to '{DEFAULT_TOOLS_BRANCH}'), then ask for that branch again.")

    def explain_update_error(self, message: str, asked_tools_branch: bool) -> str:
        """Say what state the server is in, which the bare message of server_ctl.py does not.

        The server refuses a tools-branch switch (dirty tree, diverged branch,
        unknown branch...) before it stops anything, so a refusal means the old
        server is still serving. The one error that says the opposite is its
        failed relaunch. Any other message is passed on untouched: its timing
        is not known.
        """
        if "old native server was stopped" in message:
            return (message + "\n\nThe previous server was stopped and the new one is not running: "
                              "nothing is serving now.")
        refused = message.startswith(("Tools checkout ", "--tools-branch ", "--tools-url "))
        if asked_tools_branch and refused:
            return message + "\n\nNothing was changed and the server that was running is still running."
        return message

    def explain_failure(self, deployment, stderr_lines):
        """server_ctl.py was an argparse error about the native options: the
        clone is on a branch that predates them."""
        said = "\n".join(stderr_lines)
        if ("invalid choice: 'install-tools'" in said
                or ("unrecognized arguments" in said and "--native" in said)):
            return (self.no_native_message(deployment) + "\n\nTechnical detail:\n    "
                    + "\n    ".join(stderr_lines))
        return None

    @staticmethod
    def no_native_message(deployment) -> str:
        return (
            f"Branch '{deployment.branch}' of {deployment.repo_url} has no native support "
            f"({NATIVE_SCRIPT} is missing), so the server cannot run on this Mac from it. Open "
            f"Advanced and set 'Branch' (and 'Repository URL' if the branch lives in a fork) to a "
            f"branch that has it, then press the button again."
        )

    # -- the host before a clone exists --------------------------------

    def probe(self, host) -> dict:
        """git, uv, and the GPU the platform has. Docker is never asked."""
        git_version, _ = host.version(["git", "--version"]) if host.which("git") else (None, None)
        return {
            "native": True,
            "git": {"available": bool(git_version), "version": git_version},
            # The one prerequisite the server only reports once it is cloned, and
            # it used to be found missing after two clones.
            "uv": host.find_uv(),
            # The keys of the Docker status, present so a reader written against
            # that contract gets an answer of the same shape; they say what is
            # true here.
            "docker": {"available": False, "version": None, "daemon": False,
                       "error": "not used by the native backend", "needs_group": False},
            "compose": {"available": False, "version": None},
            "gpu": {"nvidia_runtime": False, "nvidia_smi": bool(host.which("nvidia-smi")),
                    "gpu_access": "mps" if host.is_darwin() else None},
        }

    # -- operations only this backend has --------------------------------

    def require_support(self, deployment, progress_cb=None) -> None:
        """Refuse, before anything is modified, a branch of the server repository
        that has no native backend (scripts/server_ctl_native.py).

        A clone that does not exist yet is checked by `clone()` once it is made.
        For an existing clone that is on another branch the requested one is
        fetched into a private ref and read there: the clone's branches, tree
        and remote stay as they were. A check that cannot be made (offline, no
        `origin`) does not block: the explanation of an old server's argparse
        error then covers it.
        """
        if not deployment.is_cloned:
            return
        code, current = deployment._git(deployment.install_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code == 0 and current == deployment.branch:
            if os.path.isfile(os.path.join(deployment.install_dir, NATIVE_SCRIPT)):
                return
            raise DeploymentError(self.no_native_message(deployment) + " Nothing was changed.")
        emit(progress_cb, f"Checking that branch '{deployment.branch}' has native support...")
        code, _out = deployment._git(deployment.install_dir, [
            "fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{deployment.branch}:{PROBE_REF}"])
        if code != 0:
            emit(progress_cb, f"Could not check branch '{deployment.branch}' for native support "
                              f"(fetch failed); continuing without that check.")
            return
        code, _out = deployment._git(deployment.install_dir, ["cat-file", "-e", f"{PROBE_REF}:{NATIVE_SCRIPT}"])
        if code != 0:
            raise DeploymentError(self.no_native_message(deployment) + " Nothing was changed.")

    def before_update(self, deployment, progress_cb=None) -> None:
        deployment.require_native_support(progress_cb=progress_cb)
        deployment.clone_tools(progress_cb=progress_cb)

    def managed_repositories(self, deployment) -> list:
        return Backend.managed_repositories(self, deployment) + [
            ("tools repository", deployment.tools_dir, deployment.tools_branch, deployment.tools_cloned)]

    def clone_tools(self, deployment, progress_cb=None) -> None:
        """Clone the tools' repository (SADT-VISOR).

        Beside the server's clone, never inside it: a nested checkout would
        leave the server's working tree dirty and `update` refuses to pull over
        that. An existing checkout is reused as it is.
        """
        if deployment.tools_cloned:
            return
        if not shutil.which("git"):
            raise DeploymentError("git is not installed. On macOS: xcode-select --install")
        tools_dir = deployment.tools_dir
        if os.path.exists(tools_dir) and os.listdir(tools_dir):
            raise DeploymentError(
                f"{tools_dir} already exists and has no tools/ folder in it. Move it aside "
                f"or point the tools folder somewhere else.")
        emit(progress_cb, f"Cloning {deployment.tools_repo_url} ({deployment.tools_branch}) into {tools_dir}...")
        returncode, _out = deployment._spawn(
            ["git", "clone", "--branch", deployment.tools_branch, deployment.tools_repo_url, tools_dir],
            progress_cb,
        )
        if returncode != 0 or not deployment.tools_cloned:
            raise DeploymentError(
                f"Cloning {deployment.tools_repo_url} failed. See the log for git's message.")

    def install_tools(self, deployment, progress_cb=None, tools=None, force=False) -> dict:
        """Build the tools' Python environments (`server_ctl.py install-tools
        --native`). Slow the first time (several GB), a no-op afterwards."""
        args = ["install-tools"] + self.common_args(deployment)
        for tool in tools or []:
            args += ["--tool", tool]
        if force:
            args.append("--force")
        return deployment.run_ctl(args, progress_cb)

    def after_clone(self, deployment) -> None:
        if not os.path.isfile(os.path.join(deployment.install_dir, NATIVE_SCRIPT)):
            raise DeploymentError(self.no_native_message(deployment))

    def client_url(self, result):
        return public_client_url(result) or local_client_url(result)

    def no_url_message(self) -> str:
        return NO_LOCAL_TLS_MESSAGE

    # -- the grid ---------------------------------------------------------

    def checks(self, status, facts=None) -> list:
        facts = facts or {}
        git_ok, git_text = self._git_check(status)
        git_remedy = None if git_ok else Remedy("git", "Install the developer tools")
        uv_known = "uv" in status
        uv_ok = bool(status.get("uv")) if uv_known else True
        uv_text = (status.get("uv") or "not installed") if uv_known else "not checked yet"
        uv_remedy = None if uv_ok else Remedy("uv", "Install uv")
        gpu = status.get("gpu") or {}
        if gpu.get("gpu_access") == "mps" or facts.get("apple_silicon"):
            gpu_ok, gpu_text = True, "Apple GPU (Metal), used by the server directly"
        else:
            gpu_ok, gpu_text = False, "no Apple GPU: the server will run on the CPU"

        rows = [
            Check("git", "Git", git_ok, git_text, git_remedy, True),
            Check("uv", "uv", uv_ok, uv_text, uv_remedy, True),
            Check("gpu", "GPU", gpu_ok, gpu_text, None, False),
        ]
        free = facts.get("disk_free")
        if free is not None:
            enough = free >= NATIVE_SETUP_BYTES or bool(status.get("cloned"))
            rows.append(Check(
                "disk", "Disk", enough,
                f"{human_size(free)} free (about {human_size(NATIVE_SETUP_BYTES)} needed to set up)",
                None, False))
        port = self._port_check(facts)
        if port is not None:
            rows.append(Check("port", "Port", port[0], port[1], None, False))
        clone_ok, clone_text = self._clone_check(status)
        health_ok, health_text = self._health_check(status)
        rows += [
            Check("clone", "Server repository", clone_ok, clone_text, None, False),
            Check("health", "Server", health_ok, health_text, None, False),
        ]
        return rows

    # -- install / start --------------------------------------------------

    def install_plan(self, status) -> list:
        """The steps still to do, from what `status` shows is done. Each is
        idempotent, so a second press resumes after a failure or a cancel, and a
        step already satisfied is left out rather than run for nothing: starting
        an installed server no longer re-checks every tool environment."""
        status = status or {}
        tools = status.get("tools") or {}
        venvs = tools.get("venvs") or {}

        def support(deployment, progress_cb, _options):
            deployment.require_native_support(progress_cb=progress_cb)

        def clone(deployment, progress_cb, _options):
            deployment.clone(progress_cb=progress_cb)

        def clone_tools(deployment, progress_cb, _options):
            deployment.clone_tools(progress_cb=progress_cb)

        def install_tools(deployment, progress_cb, _options):
            deployment.install_tools(progress_cb=progress_cb)

        def start(deployment, progress_cb, options):
            return deployment.up(progress_cb=progress_cb, port=options.get("port"),
                                 bind=options.get("bind"))

        steps = [Step("support", "Check the server version", 1, support)]
        if not status.get("cloned"):
            steps.append(Step("clone", "Download the server", 2, clone))
        if not status.get("cloned") or not tools.get("present"):
            steps.append(Step("tools", "Download the tools", 2, clone_tools))
        if not (venvs and all(venvs.values())):
            steps.append(Step("environments", "Prepare the tools", 10, install_tools))
        steps.append(Step("start", "Start", 2, start))
        return steps

    # -- sentences ----------------------------------------------------------

    def hint(self, kind: str, status, facts=None, developer: bool = False) -> str:
        status = status or {}
        clone = status.get("clone") or {}
        if kind == "needs_setup":
            missing = [check.label for check in self.checks(status, facts)
                       if check.blocking and not check.ok]
            return ("This Mac is missing: {0}. Press the button below to see how to fix it, then "
                    "check again.").format(", ".join(missing) or "something")
        if kind == "not_installed":
            tools = status.get("tools") or {}
            if status.get("cloned") and tools.get("present"):
                missing = sorted(name for name, built in (tools.get("venvs") or {}).items() if not built)
                if missing:
                    return ("Some tools are not prepared yet ({0}). Press 'Set up VISOR Local': it "
                            "builds them, then starts the server.").format(", ".join(missing))
            return ("Everything this Mac needs is here. Setting up downloads about {0} and takes a "
                    "while the first time. You can keep using Slicer meanwhile."
                    ).format(human_size(NATIVE_SETUP_BYTES))
        if kind == "installing":
            return "Setting up. You can keep using Slicer meanwhile."
        if kind == "broken":
            return "The install in this folder cannot be used. Check the log, or choose another folder."
        if kind == "port_taken":
            return ("Another program is using the port VISOR Local needs. Press 'Try another port', "
                    "or stop that program.")
        if kind in ("installed_stopped", "starting", "running"):
            if clone.get("branch_mismatch") and not developer:
                return "A newer version is available. Press 'Update VISOR Local'."
            if clone.get("branch_mismatch"):
                return (f"The clone is on branch '{clone.get('branch')}' but this deployment asks for "
                        f"'{clone.get('configured_branch')}'. Press 'Update VISOR Local' to switch it.")
            if clone.get("behind"):
                return "A newer version of the server is available. Press 'Update VISOR Local'."
        if kind == "installed_stopped":
            return "VISOR Local is installed but not running. Press 'Start VISOR Local'."
        if kind == "starting":
            return ("VISOR Local is starting; the first start can take a few minutes."
                    + (" Check the Log." if developer else ""))
        return "VISOR Local is running on this Mac. Open any tool (ALI, AMASSS, ...) to use it."


def _has_nvidia_cdi_device(raw) -> bool:
    """True when docker has discovered a CDI device for an nvidia GPU.

    The entries look like `{"Source": "cdi", "ID": "nvidia.com/gpu=all"}`. The
    vendor prefix is what identifies them: the same list carries every other CDI
    vendor registered on the host. Mirrors the function of the same name in
    scripts/server_ctl.py.
    """
    try:
        devices = json.loads(raw or "null")
    except (TypeError, ValueError):
        return False
    if not isinstance(devices, list):
        return False
    return any(
        isinstance(device, dict)
        and device.get("Source") == "cdi"
        and str(device.get("ID", "")).startswith("nvidia.com/gpu")
        for device in devices
    )


DOCKER = DockerBackend()
NATIVE = NativeBackend()
