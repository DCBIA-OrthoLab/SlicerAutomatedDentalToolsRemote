"""Driving a local tool-server deployment: clone, docker, start, update, data.

Same dependency rule as `client.py` (see ARCHITECTURE.md): **this module
imports neither `slicer` nor `qt`**, so it runs — and is unit-tested — in plain
CI with no Slicer interpreter. Everything it does is `subprocess` plus the
standard library, and `ServerToolsCoreLib.tls` (standard library too) for the
one request it makes, so the HTTPS-only rule and the out-of-band loopback pin
(tls.py, rule 3) are the client's own rather than a copy.

Two backends (backends.py), chosen once by `select_backend()`. Docker is the
default everywhere. On an Apple-silicon Mac Docker cannot reach the GPU, so the
server runs as a plain process (`--native`, see `prefer_native()`), serving HTTPS
on 127.0.0.1 with a certificate this module makes locally (local_cert.py) and
the client pins. Everything that differs between the two is a method of the
backend; this class does what is the same: run a process, stream its log,
cancel, and drive git.

It is deliberately thin. The real logic lives in the server repository's
`scripts/server_ctl.py`, which this class clones and then calls: the panel and
the terminal therefore do exactly the same thing, and a fix to the deployment
logic ships with the server rather than needing a new extension release. The
one thing that cannot be delegated is the bootstrap — you cannot run a script
out of a clone that does not exist yet — so `probe_host()` and `clone()` are
implemented here and nothing else is.

`server_ctl.py --json` prints one JSON object on stdout and narrates on
stderr, which is what lets `run_ctl` stream a live log into the GUI *and*
return a parsed result from the same call.
"""

import collections
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.error

from ServerToolsCoreLib import tls

from . import backends, local_cert
from .backends import (  # noqa: F401 - re-exported: this is where callers have always looked
    BIND_EVERYWHERE,
    BIND_LOCALHOST,
    DEFAULT_SERVER_URL,
    DEFAULT_TOOLS_BRANCH,
    DEFAULT_TOOLS_REPO_URL,
    NATIVE_SCRIPT,
    NO_LOCAL_TLS_MESSAGE,
    NO_PUBLIC_URL_MESSAGE,
    BranchSwitch,
    DeploymentError,
    _has_nvidia_cdi_device,
    local_client_url,
)
from .backends import emit as _emit

# How much of a failed command's output travels with its error message.
_ERROR_TAIL_LINES = 12

DEFAULT_REPO_URL = "https://github.com/DCBIA-OrthoLab/VISOR-serve.git"
DEFAULT_BRANCH = "main"
# Where the container listens on this machine, for DISPLAY only, is
# DEFAULT_SERVER_URL (backends.py). The client never uses it for a Docker
# deployment: the certificate the container presents there is self-signed and
# nothing pins it, and the client accepts only a publicly-signed one
# (ServerToolsCoreLib/tls.py). What the client is given is the deployment's
# public URL, or for a native server the pinned local address: see
# `Backend.client_url`.

# Where a clone goes when the user has not said. The home directory, not
# anywhere under the Slicer install: this holds a git clone plus up to ~29 GB
# of model weights, and it must survive an extension update or a Slicer
# reinstall.
DEFAULT_INSTALL_DIR = os.path.join(os.path.expanduser("~"), "SlicerCloudServer")


_PROBE_REF = backends.PROBE_REF


# The environment every subprocess here is given. None means "inherit", which
# is right outside Slicer and WRONG inside it -- see set_subprocess_env().
_SUBPROCESS_ENV = None


def set_subprocess_env(env) -> None:
    """Use `env` for every process this module spawns. Call it with
    `slicer.util.startupEnvironment()` before anything else.

    This is not a nicety, it is the difference between working and not. Slicer's
    launcher exports its own PYTHONHOME/PYTHONPATH, so a subprocess running the
    SYSTEM python3 starts up against SLICER's standard library. Measured on a
    fresh install: python3.10 loading /opt/slicer/lib/Python/lib/python3.12/
    dies with "AssertionError: SRE module mismatch" on `import argparse` --
    before a single line of server_ctl.py runs, with an empty stdout and exit
    code 1. LD_LIBRARY_PATH alone is harmless; PYTHONHOME is what kills it.

    `slicer.util.startupEnvironment()` returns the environment as it was before
    the launcher touched it, which is the same thing Slicer's own
    `bin/exec-outside-slicer-env.sh` reconstructs for wrapped binaries.

    Passing None restores plain inheritance, which is what the unit tests use.
    """
    global _SUBPROCESS_ENV
    _SUBPROCESS_ENV = dict(env) if env else None


def _no_window_kwargs() -> dict:
    """Keep Windows from flashing a console window for every subprocess."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _subprocess_kwargs() -> dict:
    """Everything every spawn in this module must pass. One place, because a
    site that forgets the environment fails only inside Slicer."""
    kwargs = _no_window_kwargs()
    if _SUBPROCESS_ENV is not None:
        kwargs["env"] = _SUBPROCESS_ENV
    return kwargs


def find_python() -> str:
    """A Python 3 interpreter able to run `server_ctl.py` (standard library only).

    `sys.executable` is checked LAST and only when it looks like a Python
    binary. Inside Slicer it can be the Slicer application itself, and handing
    that to `subprocess` would launch a second Slicer instead of running a
    script — a failure mode with no useful error message at all.
    """
    candidates = [shutil.which("python3"), shutil.which("python")]

    # Slicer ships its own interpreter next to the app binary; on a Windows or
    # macOS host with no system Python it is the only one there is.
    here = os.path.dirname(os.path.abspath(sys.executable))
    for name in ("PythonSlicer", "PythonSlicer.exe"):
        candidate = os.path.join(here, name)
        if os.path.isfile(candidate):
            candidates.append(candidate)

    if os.path.basename(sys.executable).lower().startswith("python"):
        candidates.append(sys.executable)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            completed = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[0])"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=30, check=False, **_subprocess_kwargs()
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout.strip() == "3":
            return candidate

    raise DeploymentError(
        "No Python 3 interpreter was found. Install Python 3 and make sure it is in PATH "
        "(Debian/Ubuntu: sudo apt-get install -y python3)."
    )


def host_addresses():
    """Addresses this machine could publish the server on, worst risk last.

    Returned as `(address, label, kind)` with `kind` in "loopback" / "vpn" /
    "lan" / "all", so the panel can both label an entry and warn about it
    without knowing anything about networking.

    A VPN address is singled out rather than lumped in with the rest because it
    is a genuinely different risk: publishing on a Tailscale/WireGuard address
    reaches only the machines in that private mesh, over an encrypted link,
    while the same server on a LAN address presents a self-signed certificate
    that no other machine can verify. The two must not look like the same
    choice in a list.

    Docker's own bridges are skipped: binding the server to `docker0` publishes
    it to containers and nothing else, which is never what anyone means.
    """
    found = [(BIND_LOCALHOST, "This computer only", "loopback")]
    try:
        completed = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=10, check=False, **_subprocess_kwargs()
        )
        lines = completed.stdout.splitlines() if completed.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        lines = []

    for line in lines:
        parts = line.split()
        # "2: eth0    inet 192.168.1.10/24 brd ... scope global eth0"
        if len(parts) < 4 or parts[2] != "inet":
            continue
        interface, address = parts[1], parts[3].split("/")[0]
        if address.startswith("127.") or interface == "lo":
            continue
        if interface.startswith(("docker", "br-", "veth", "virbr")):
            continue
        if interface.startswith(("tailscale", "wg", "tun", "zt")):
            found.append((address, f"{interface} — private network, encrypted", "vpn"))
        else:
            found.append((address, f"{interface} — local network", "lan"))

    found.append((BIND_EVERYWHERE, "Every network on this machine", "all"))
    return found


def is_apple_silicon(system=None, machine=None, sysctl=None) -> bool:
    """Whether this is a Mac with an Apple-silicon CPU, even from an x86_64
    process: the stable Slicer for macOS runs under Rosetta, where
    `platform.machine()` says x86_64 and `sysctl hw.optional.arm64` still says 1.
    The three arguments exist so a test can stand in for the machine."""
    if (system or sys.platform) != "darwin":
        return False
    if (machine or platform.machine()).lower() in ("arm64", "aarch64"):
        return True
    if sysctl is None:
        try:
            done = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, timeout=10, check=False,
                                  **_subprocess_kwargs())
            sysctl = done.stdout.strip() if done.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            sysctl = ""
    return str(sysctl).strip() == "1"


def prefer_native() -> bool:
    """The native backend is the right one here: a Mac with Apple silicon,
    where Docker cannot give a container the GPU. Everywhere else Docker stays
    the default; an Intel Mac is not served by the native backend either."""
    return is_apple_silicon()


def select_backend(native=None):
    """The backend for this machine: chosen ONCE, here, and never re-derived.

    `native` is an explicit choice (tests, a terminal user, a developer forcing
    the other one); None means "decide from the platform". Every later
    difference between the two is a method of the object returned, not a
    conditional somewhere else.
    """
    if native is None:
        native = prefer_native()
    return backends.NATIVE if native else backends.DOCKER


def find_uv():
    """Where `uv` is, or None. Same places as `find_uv()` in the server's
    native_tools.py, which is what will look for it once the server starts: the
    answer has to be the same before the clone exists as after. A GUI
    application does not inherit the shell's PATH, and uv's own installer puts
    it in ~/.local/bin."""
    found = os.environ.get("UV") or shutil.which("uv")
    if found:
        return found
    for candidate in (os.path.expanduser("~/.local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class _Host:
    """What a backend may ask of this machine before any clone exists. One
    place for the subprocess environment, so a probe cannot forget it."""

    @staticmethod
    def version(command):
        try:
            completed = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=30, check=False, **_subprocess_kwargs()
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)
        if completed.returncode != 0:
            return None, (completed.stderr or completed.stdout).strip()
        return completed.stdout.strip(), None

    @staticmethod
    def which(name):
        return shutil.which(name)

    @staticmethod
    def find_uv():
        return find_uv()

    @staticmethod
    def is_darwin():
        return sys.platform == "darwin"


def probe_host(native: bool = False) -> dict:
    """The prerequisites, answered without a clone.

    Only ever used before `scripts/server_ctl.py` exists locally; once it does,
    `status()` delegates to it so the panel and the terminal never disagree
    about what "docker is fine" means. What is asked is the backend's business
    (`Backend.probe`): git, docker, compose and the GPU for Docker; git and uv
    for the native one.
    """
    return select_backend(native).probe(_Host())


def free_space(path):
    """Free bytes on the disk that holds `path` (or the nearest folder that exists), or None."""
    probe = os.path.abspath(path or ".")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def port_in_use(port, host="127.0.0.1", timeout=0.5):
    """Whether something already accepts connections on `host:port`. A refused
    connection is the normal answer and means the port is free."""
    import socket  # noqa: PLC0415 - only this function needs it
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def find_free_port(start=8001, stop=8100, host="127.0.0.1"):
    """The first port in `start..stop` that nothing is listening on, or None."""
    for port in range(start, stop):
        if not port_in_use(port, host):
            return port
    return None


def client_url(result):
    """The URL the CLIENT may use for a result of either backend, or None: the
    deployment's public address, else a native server's pinned local one.

    Kept for callers that have no deployment in hand; the panel asks its
    deployment's backend (`Backend.client_url`), which never offers a local
    address for Docker.
    """
    return backends.NATIVE.client_url(result)


def describe_update(result) -> str:
    """One sentence saying what an `update` did, for either backend's result.

    The two answer differently: Docker reports `pulled`/`recreated`, the native
    server `pulled`, `tools_pulled`, `venvs` (one entry per tool environment,
    `action` "built" when it was rebuilt) and `restarted`. Reading only the
    Docker keys made a native update that had pulled the tools and relaunched
    the server say "nothing to do".
    """
    result = result or {}
    clone = result.get("clone") or {}
    rebuilt = sum(1 for entry in (result.get("venvs") or [])
                  if isinstance(entry, dict) and entry.get("action") == "built")
    relaunched = bool(result.get("restarted") or result.get("recreated"))
    # A Docker update always relaunches after a pull and has no `restarted` key.
    if result.get("pulled") and "restarted" not in result:
        relaunched = True
    changes = []
    if result.get("pulled"):
        changes.append(f"Updated to {clone.get('commit') or 'the latest commit'}")
        if result.get("tools_pulled"):
            changes[0] += " and the tools"
    elif result.get("tools_pulled"):
        changes.append("Tools updated")
    if rebuilt:
        changes.append(f"{rebuilt} tool environment(s) rebuilt")
    if not changes:
        if not relaunched:
            return "Already up to date and running, nothing to do."
        what = "the container" if result.get("recreated") else "the server"
        return f"Already up to date; {what} was relaunched."
    sentence = "; ".join(changes)
    if not relaunched:
        return sentence + "."
    if result.get("pulled") and not result.get("tools_pulled") and not rebuilt:
        return sentence + " and relaunched."
    return sentence + "; the server was relaunched."


def tool_names(payload):
    """Tool names out of a `GET /tools` body, or None if it is not one.

    The server sends a list of `{name, arguments, output_kind}`; a name-keyed
    mapping is accepted too, since that is the shape `ToolServerClient` hands
    around internally and the two are easy to confuse. Anything else is None
    — "unknown" is a state the panel renders, a wrong list is not.
    """
    if isinstance(payload, dict):
        return sorted(payload)
    if isinstance(payload, list):
        return sorted(entry["name"] for entry in payload if isinstance(entry, dict) and "name" in entry)
    return None


class LocalServerDeployment:
    """One local clone of the server repository, and the deployment run from it."""

    def __init__(self, install_dir=None, repo_url=DEFAULT_REPO_URL, branch=DEFAULT_BRANCH,
                 python_executable=None, native=None, tools_repo_url=DEFAULT_TOOLS_REPO_URL,
                 tools_branch=DEFAULT_TOOLS_BRANCH, tools_dir=None):
        self.install_dir = install_dir or DEFAULT_INSTALL_DIR
        self.repo_url = repo_url
        self.branch = branch
        self._python = python_executable
        # None means "decide from this machine" (select_backend); True or False
        # is an explicit choice, which is what the tests and a terminal user use.
        # Chosen here, once: nothing below asks the platform again.
        self.backend = select_backend(native)
        self.tools_repo_url = tools_repo_url
        self.tools_branch = tools_branch
        self._tools_dir = tools_dir
        # The subprocess currently running, so cancel() can reach it. A tool
        # download is hours long; a Cancel button that only stops *listening*
        # would leave 12 GB still coming down the wire.
        self._process = None
        self._cancelled = False
        self._last_stderr = []

    @property
    def native(self) -> bool:
        return self.backend.native

    # -- paths ---------------------------------------------------------

    @property
    def script_path(self) -> str:
        return os.path.join(self.install_dir, "scripts", "server_ctl.py")

    @property
    def install_docker_script(self) -> str:
        return os.path.join(self.install_dir, "scripts", "install-docker.sh")

    @property
    def tools_dir(self) -> str:
        """The SADT-VISOR checkout the native backend builds its tool
        environments from: the folder beside the install folder, which is also
        where server_ctl.py looks when it is not told."""
        return self._tools_dir or os.path.join(
            os.path.dirname(os.path.abspath(self.install_dir)), "SADT-VISOR")

    @property
    def tools_cloned(self) -> bool:
        return os.path.isdir(os.path.join(self.tools_dir, "tools"))

    @property
    def data_dir(self) -> str:
        return os.path.join(self.install_dir, "DATA")

    @property
    def is_cloned(self) -> bool:
        """A clone we can actually drive — the control script has to be there.

        Not `os.path.isdir(install_dir)`: a half-finished clone, or a folder
        the user made by hand, is not a deployment and pretending otherwise
        turns every later call into an obscure "file not found".
        """
        return os.path.isfile(self.script_path)

    @property
    def python(self) -> str:
        if self._python is None:
            self._python = find_python()
        return self._python

    # -- process plumbing ----------------------------------------------

    def cancel(self) -> None:
        """Terminate whatever is running. Safe to call from another thread."""
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _ctl_env(self) -> dict:
        """The environment for every `server_ctl.py` this deployment starts: the
        one Slicer must hand to subprocesses, plus the backend NAMED (see
        `Backend.subprocess_env`)."""
        base = _SUBPROCESS_ENV if _SUBPROCESS_ENV is not None else os.environ
        return self.backend.subprocess_env(base)

    def _spawn(self, command, progress_cb=None, cwd=None, env=None):
        """Run `command`, streaming stderr to `progress_cb`, returning (rc, stdout).

        stderr is drained by its own thread while this one reads stdout. Both
        are pipes, and reading them one after the other deadlocks the moment
        the writer fills the one nobody is reading — which is exactly what a
        long `docker compose up` does.
        """
        self._cancelled = False
        _emit(progress_cb, "$ " + " ".join(command))
        kwargs = dict(_subprocess_kwargs())
        if env is not None:
            kwargs["env"] = env
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, **kwargs
            )
        except OSError as exc:
            raise DeploymentError(f"Could not run {command[0]}: {exc}") from None

        self._process = process

        # Kept as well as streamed: when the command fails, the last lines of
        # what it said ARE the diagnosis, and an error dialog reading only
        # "exit code 1" sends the user hunting through a log pane they may not
        # have opened. Bounded, because `docker compose up` prints tens of
        # thousands of layer-progress lines.
        tail = collections.deque(maxlen=_ERROR_TAIL_LINES)

        def drain_stderr():
            for line in process.stderr:
                line = line.rstrip()
                tail.append(line)
                _emit(progress_cb, line)

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        try:
            out = process.stdout.read()
        finally:
            process.wait()
            reader.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
            self._process = None

        if self._cancelled:
            raise DeploymentError("Cancelled.")
        self._last_stderr = [line for line in tail if line.strip()]
        return process.returncode, out

    def run_ctl(self, args, progress_cb=None):
        """Call `scripts/server_ctl.py --json <args>` and return its parsed result."""
        if not self.is_cloned:
            raise DeploymentError(
                f"The server repository is not installed in {self.install_dir} yet. "
                f"Use 'Install and start' first."
            )
        command = [self.python, self.script_path, "--json"] + list(args)
        returncode, out = self._spawn(command, progress_cb, cwd=self.install_dir, env=self._ctl_env())

        if returncode != 0:
            explained = self.backend.explain_failure(self, self._last_stderr)
            if explained:
                raise DeploymentError(explained)

        try:
            result = json.loads(out) if out.strip() else {}
        except ValueError:
            raise DeploymentError(self._failure_message(args, returncode, "produced no usable result"))
        if "error" in result:
            raise DeploymentError(result["error"])
        if returncode != 0:
            raise DeploymentError(self._failure_message(args, returncode, "failed"))
        return result

    def _failure_message(self, args, returncode: int, what: str) -> str:
        """The error the user actually sees, with the tail of what the command said.

        Without this it read "server_ctl.py status --branch docker failed (exit
        code 1)" and nothing else — true, useless, and the reason a real
        failure on a fresh machine could not be diagnosed from the report.
        """
        message = f"server_ctl.py {' '.join(args)} {what} (exit code {returncode})."
        if self._last_stderr:
            message += "\n\nIt printed:\n    " + "\n    ".join(self._last_stderr)
        else:
            message += " It printed nothing at all."
        return message

    # -- backend plumbing ------------------------------------------------

    def ensure_certificate(self, progress_cb=None) -> dict:
        """The pinned certificate and key, made first when there is none or the
        one there expires soon (local_cert.ensure_local_certificate)."""
        try:
            return local_cert.ensure_local_certificate(
                subprocess_kwargs=_subprocess_kwargs(), progress_cb=lambda m: _emit(progress_cb, m))
        except local_cert.CertificateError as exc:
            raise DeploymentError(str(exc)) from None

    def _git(self, path, args, timeout=120):
        """Run git in `path`; (returncode, stdout). Never raises for a failing git."""
        try:
            done = subprocess.run(
                ["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=timeout, check=False, **_subprocess_kwargs())
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return done.returncode, done.stdout.strip()

    def require_native_support(self, progress_cb=None) -> None:
        """Refuse, before anything is modified, a branch of the server repository
        that this backend cannot run (the native one needs
        scripts/server_ctl_native.py). Docker has nothing to check."""
        self.backend.require_support(self, progress_cb)

    def pending_branch_switches(self) -> list:
        """The managed clones (server, and tools in native mode) that sit on
        another branch than the one asked for, with whether their tree is dirty.

        Read-only. The panel asks before it lets anything switch them, and
        refuses outright on a dirty tree. Untracked files do not count: they do
        not stand in a checkout's way.
        """
        found = []
        for what, path, wanted, present in self.backend.managed_repositories(self):
            if not present:
                continue
            code, current = self._git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
            if code != 0 or current == wanted:
                continue
            _code, dirty = self._git(path, ["status", "--porcelain", "--untracked-files=no"])
            found.append(BranchSwitch(what, path, current, wanted, bool(dirty)))
        return found

    def current_branch(self, path) -> str:
        return self._git(path, ["rev-parse", "--abbrev-ref", "HEAD"])[1]

    def clone_tools(self, progress_cb=None) -> None:
        """Clone the tools' repository where the backend uses one (no-op for Docker)."""
        self.backend.clone_tools(self, progress_cb)

    def install_tools(self, progress_cb=None, tools=None, force: bool = False) -> dict:
        """Build the tools' Python environments (native backend only)."""
        return self.backend.install_tools(self, progress_cb, tools, force)

    # -- operations ----------------------------------------------------

    def clone(self, progress_cb=None) -> None:
        """Clone the server repository into `install_dir`.

        Refuses a non-empty destination that is not already this clone —
        `git clone` would fail on it anyway, but with a message about an
        existing directory rather than about what to do next.
        """
        if self.is_cloned:
            _emit(progress_cb, f"Already installed in {self.install_dir}.")
            return
        if not shutil.which("git"):
            raise DeploymentError(
                "git is not installed. Install it and restart Slicer.\n"
                "  Debian/Ubuntu: sudo apt-get install -y git\n"
                "  macOS:         xcode-select --install\n"
                "  Windows:       https://git-scm.com/download/win"
            )
        if os.path.isdir(os.path.join(self.install_dir, ".git")):
            raise DeploymentError(
                f"{self.install_dir} is a git clone but carries no scripts/server_ctl.py. "
                f"It is probably a different repository, or a branch predating it — check it, "
                f"or choose another folder."
            )
        if os.path.exists(self.install_dir) and os.listdir(self.install_dir):
            raise DeploymentError(
                f"{self.install_dir} already exists and is not empty. Choose an empty folder "
                f"or move that one aside."
            )

        parent = os.path.dirname(os.path.abspath(self.install_dir))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        _emit(progress_cb, f"Cloning {self.repo_url} ({self.branch}) into {self.install_dir}...")
        returncode, _out = self._spawn(
            ["git", "clone", "--branch", self.branch, self.repo_url, self.install_dir],
            progress_cb,
        )
        if returncode != 0 or not self.is_cloned:
            raise DeploymentError(
                f"Cloning {self.repo_url} failed. If the repository is private, set up a git "
                f"credential helper or an SSH key first — see the log for git's own message."
            )
        self.backend.after_clone(self)

    def can_install_docker(self) -> bool:
        """Whether a root helper can be run from here, without a terminal.

        Linux with a graphical `pkexec` only. Everywhere else the answer is a
        GUI installer (Docker Desktop) or a root shell, and offering a button
        that cannot deliver either is worse than saying so.

        Named for Docker because that was its first caller; it is really the
        predicate for *any* administrator action this panel offers, and
        `enable_gpu_runtime` gates on it too. Kept under one name rather than
        two so there is a single answer to "can this panel ask for a password".
        """
        if sys.platform.startswith("linux") and os.geteuid() == 0:
            return True
        return (
            sys.platform.startswith("linux")
            and bool(shutil.which("pkexec"))
            and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        )

    def _run_install_script(self, extra_args, failed_what: str, progress_cb=None) -> None:
        """Run the repository's install-docker.sh with administrator rights.

        The clone has to exist first — the script is in it. Deliberately not a
        curl-pipe-to-root from the GUI: what runs as root is a file the user
        already has on disk and can read.

        Shared by the two things the panel can ask root for, so the pkexec
        handling, the "no graphical helper" fallback and the terminal command
        printed on failure cannot drift between them. `failed_what` is the only
        difference: an error has to name the action that failed, not the script.
        """
        if not os.path.isfile(self.install_docker_script):
            raise DeploymentError(
                "scripts/install-docker.sh is missing from the install — clone the server "
                "repository first."
            )
        if not sys.platform.startswith("linux"):
            raise DeploymentError(
                "Automatic installation is Linux only. On macOS and Windows, install Docker "
                "Desktop from https://docs.docker.com/get-docker/, start it, and come back here."
            )

        command = ["sh", self.install_docker_script] + list(extra_args)
        by_hand = " ".join(["sudo", "sh", self.install_docker_script] + list(extra_args))
        if os.geteuid() != 0:
            if not shutil.which("pkexec"):
                raise DeploymentError(
                    "Administrator rights are needed and no graphical helper (pkexec) is "
                    f"available. Run this in a terminal instead:\n\n    {by_hand}"
                )
            command = ["pkexec"] + command

        _emit(progress_cb, "A password prompt will appear — this needs root.")
        returncode, _out = self._spawn(command, progress_cb)
        if returncode != 0:
            raise DeploymentError(
                f"{failed_what} failed or was refused. The log has the script's own "
                f"output. You can also run it by hand:\n\n    {by_hand}"
            )

    def install_docker(self, progress_cb=None, with_nvidia: bool = False) -> None:
        """Install Docker Engine, and optionally the NVIDIA Container Toolkit."""
        self._run_install_script(
            ["--nvidia"] if with_nvidia else [],
            "Installing Docker",
            progress_cb,
        )

    def enable_gpu_runtime(self, progress_cb=None) -> None:
        """Let docker reach the GPU, from the panel rather than from a terminal.

        The same script under `--nvidia`, which installs the container toolkit
        if it is absent and then registers the `nvidia` runtime with docker.
        On a host that already has the toolkit — the common case now that it
        generates CDI specs by itself — it skips straight to the registration.

        **This restarts the docker daemon**, so every running container stops,
        the tool server included. Unlike installing Docker, which happens on a
        machine where nothing is up yet, that is a real consequence and the
        caller has to have said so before the password prompt appears.
        """
        self._run_install_script(["--nvidia"], "Enabling GPU support", progress_cb)

    def status(self, check_remote: bool = False, progress_cb=None) -> dict:
        """Everything the panel shows, in one shape whether or not it is cloned."""
        if not self.is_cloned:
            probed = self.backend.probe(_Host())
            # `gpu` is deliberately NOT overridden here: probe_host() answers it
            # for real. It used to be pinned to nvidia_runtime=False right on
            # this line, so the pre-install screen — the only one a new user
            # sees before pressing Install — told everyone their card was
            # unusable, whatever the machine actually had.
            probed.update({
                "cloned": False,
                "repo_root": self.install_dir,
                "clone": {"is_git_repo": False, "behind": 0, "error": "not installed yet"},
                "container": {"running": False, "state": None},
                "server": {"url": DEFAULT_SERVER_URL, "healthy": False},
                "env": {"has_token": False},
            })
            return probed

        args = ["status"] + self.backend.common_args(self) + ["--branch", self.branch]
        if check_remote:
            args.append("--check-remote")
        result = self.run_ctl(args, progress_cb)
        result["cloned"] = True
        return result

    def up(self, progress_cb=None, force_recreate: bool = False, port=None, bind=None) -> dict:
        """Start the server. The result carries the URL and the API token.

        `port` is only needed when something else already holds the default —
        it is remembered in the deployment's `.env`, so it has to be passed
        once, not on every start.

        `bind` is which address the port is published on: None keeps whatever
        the deployment already uses (BIND_LOCALHOST on a first install), and
        BIND_EVERYWHERE — the empty string — is a real value, not "unset", so
        this tests it with `is not None`.
        """
        args = ["up"] + self.backend.common_args(self, bind) + self.backend.start_args(self, progress_cb)
        if port:
            args += ["--port", str(port)]
        if bind is not None:
            args += ["--bind", bind]
        if force_recreate:
            args.append("--force-recreate")
        return self.run_ctl(args, progress_cb)

    def update(self, progress_cb=None, force: bool = False, bind=None) -> dict:
        """Fetch, fast-forward, relaunch — and put the clone on `self.branch`.

        The branch is passed on every update, not only when it changed: a
        clone is created once, so a deployment pointed at another branch
        afterwards would otherwise keep following the old one for ever, in
        silence. This is what makes the Branch field mean something after the
        first install.
        """
        args = ["update"] + self.backend.common_args(self, bind) + self.backend.start_args(
            self, progress_cb) + ["--branch", self.branch]
        tools_args = self.backend.update_args(self, progress_cb)
        args += tools_args
        if bind is not None:
            args += ["--bind", bind]
        if force:
            args.append("--force")
        try:
            return self.run_ctl(args, progress_cb)
        except DeploymentError as exc:
            explained = self.backend.explain_update_error(str(exc), bool(tools_args))
            if explained == str(exc):
                raise
            raise DeploymentError(explained) from None

    def _ctl_publishes(self, option: str) -> bool:
        """Whether `server_ctl.py update --help` lists `option`. False when it cannot be asked."""
        if not self.is_cloned:
            return False
        kwargs = dict(_subprocess_kwargs())
        kwargs["env"] = self._ctl_env()
        try:
            done = subprocess.run(
                [self.python, self.script_path, "update", "--help"], cwd=self.install_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
                **kwargs)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0 and option in done.stdout

    def down(self, progress_cb=None) -> dict:
        return self.run_ctl(["down"] + self.backend.common_args(self), progress_cb)

    def stop_detached(self) -> bool:
        """Ask docker to stop the container and return IMMEDIATELY.

        For the application-quit hook, where the ordinary `down()` cannot be
        used: `docker compose stop` waits out its grace period before killing
        the container, and this image's uvicorn (started with `--reload`) does
        not act on SIGTERM — so a stop measures **10.5 s**, every one of which
        would be Slicer refusing to close. Shortening the grace only trades
        that for a shorter hang; detaching removes it.

        Nothing is reported back, and that is the honest shape of the
        operation: by the time it finishes there is no window left to report
        into. The failure mode is the container staying up, which is exactly
        the state we were in anyway.

        Returns whether the stop was *launched*, not whether it succeeded.
        """
        if not self.is_cloned:
            return False
        try:
            command = [self.python, self.script_path, "down"] + self.backend.common_args(self)
        except DeploymentError:
            return False

        kwargs = dict(_subprocess_kwargs())
        kwargs["env"] = self._ctl_env()
        if os.name == "nt":
            # DETACHED_PROCESS: survives the parent, no console window.
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000008
        else:
            # Its own session, so closing Slicer's terminal cannot take it down
            # with a SIGHUP before docker has finished.
            kwargs["start_new_session"] = True

        try:
            subprocess.Popen(
                command, cwd=self.install_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                **kwargs
            )
        except OSError:
            return False
        return True

    def logs(self, lines: int = 200, progress_cb=None) -> dict:
        return self.run_ctl(["logs"] + self.backend.common_args(self) + ["-n", str(lines)], progress_cb)

    def token(self, progress_cb=None) -> str:
        return self.run_ctl(["token"], progress_cb)["token"]

    def catalog(self, progress_cb=None) -> dict:
        return self.run_ctl(["catalog"], progress_cb)

    def list_tools(self, url=None, timeout: float = 10.0):
        """Tool names **this deployment's** server exposes, or None if it is down.

        Asked directly rather than through `ServerToolsCoreLib`'s client: that
        one talks to whatever server the extension is configured against, which
        is not necessarily the deployment this panel is managing — and
        answering "which tools does it have?" about a different machine is
        worse than answering "unknown".

        `GET /tools` is the one unauthenticated endpoint besides `/health`, so
        this needs no token and stays inside the no-`requests` rule.
        """
        if not url:
            return None
        try:
            # https:// only and a publicly-signed certificate; anything else
            # (an http:// URL, the local self-signed listener) reads "unknown".
            url = tls.require_https(url)
            with tls.urlopen(f"{url}/tools", timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return tool_names(payload)

    def download_data(self, tools, progress_cb=None, force: bool = False) -> dict:
        """Fetch the selected tools' models and test files.

        An empty selection is refused rather than treated as "everything": the
        full manifest is ~29 GB, and a stray click on a button whose label says
        "Download selected" must not start that.

        Whatever is already on disk is skipped by the engine, so re-running
        after adding one tool costs only that tool, and an interrupted download
        resumes by simply being started again.
        """
        tools = [tool for tool in tools if tool]
        if not tools:
            raise DeploymentError("No tool selected. Tick at least one before downloading.")
        args = ["models"]
        for tool in tools:
            args += ["--tool", tool]
        if force:
            args.append("--force")
        return self.run_ctl(args, progress_cb)
