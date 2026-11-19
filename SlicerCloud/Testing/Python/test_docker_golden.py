"""Every command the panel builds in Docker mode, pinned byte for byte.

    python3 -m unittest test_docker_golden

Linux, Windows and an Intel Mac run the Docker backend, and nothing here can
exercise a real Docker. What can be pinned is the one thing the extension
controls: the argument vector of every process it starts. These literals were
written BEFORE the backend refactor and must not change with it. A change here
is a change of contract with `scripts/server_ctl.py`, and has to be a decision.

Only the argv and the working directory are pinned (the environment has its own
tests): `subprocess.Popen` and `subprocess.run` are replaced by recorders, so
nothing is executed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))

from SlicerCloudLib import deploy  # noqa: E402

PY = "/usr/bin/python3"


class _Pipe:
    def __init__(self, text=""):
        self._text = text

    def read(self):
        return self._text

    def __iter__(self):
        return iter(())

    def close(self):
        pass


class _Process:
    def __init__(self, out):
        self.stdout = _Pipe(out)
        self.stderr = _Pipe()
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass


def collect(deploy_module, install_dir, calls):
    """Run every Docker-mode operation against recorders; returns {name: [(argv, cwd)]}.

    `deploy_module` is a parameter so that the same script can be pointed at
    another checkout (the before/after comparison made for the refactor).
    """
    recorded = {}
    current = {"name": None}

    def record(kind, argv, cwd):
        recorded.setdefault(current["name"], []).append((kind, list(argv), cwd))

    def popen(command, cwd=None, **_kwargs):
        record("popen", command, cwd)
        if command[:2] == ["git", "clone"]:
            scripts = os.path.join(command[-1], "scripts")
            os.makedirs(scripts, exist_ok=True)
            with open(os.path.join(scripts, "server_ctl.py"), "w") as handle:
                handle.write("")
            with open(os.path.join(scripts, "install-docker.sh"), "w") as handle:
                handle.write("")
        out = json.dumps({"token": "t", "ok": True}) if "token" in command else "{}"
        return _Process(out)

    def run(command, **kwargs):
        record("run", command, kwargs.get("cwd"))
        return subprocess.CompletedProcess(command, 0, "3\n", "")

    which = lambda name: "/usr/bin/" + name  # noqa: E731
    with mock.patch.object(deploy_module.subprocess, "Popen", popen), \
            mock.patch.object(deploy_module.subprocess, "run", run), \
            mock.patch.object(deploy_module.shutil, "which", which):
        def fresh(directory=install_dir):
            return deploy_module.LocalServerDeployment(
                directory, repo_url="https://example.org/VISOR-serve.git", branch="main",
                python_executable=PY, native=False)

        def step(name, function):
            current["name"] = name
            function()

        # Not installed yet: the host is probed, then cloned.
        empty = os.path.join(install_dir, "empty")
        step("status before install", lambda: fresh(empty).status())
        step("clone", lambda: fresh(empty).clone())

        d = fresh(empty)            # now cloned by the step above
        step("status", lambda: d.status())
        step("status check remote", lambda: d.status(check_remote=True))
        step("up", lambda: d.up())
        step("up as the panel sends it", lambda: d.up(port=8000, bind="127.0.0.1"))
        step("up port", lambda: d.up(port=8123))
        step("up everywhere", lambda: d.up(bind=""))
        step("up force", lambda: d.up(force_recreate=True))
        step("update", lambda: d.update())
        step("update as the panel sends it", lambda: d.update(bind="127.0.0.1"))
        step("update force bind", lambda: d.update(force=True, bind="100.94.102.62"))
        step("down", lambda: d.down())
        step("logs", lambda: d.logs())
        step("logs 50", lambda: d.logs(50))
        step("token", lambda: d.token())
        step("catalog", lambda: d.catalog())
        step("models", lambda: d.download_data(["AMASSS", "ALI"]))
        step("models force", lambda: d.download_data(["CNE"], force=True))
        step("stop on exit", lambda: d.stop_detached())

        # The administrator scripts, as root and through pkexec.
        with mock.patch.object(deploy_module.sys, "platform", "linux"), \
                mock.patch.object(deploy_module.os, "geteuid", create=True, return_value=0):
            step("install docker as root", lambda: d.install_docker())
            step("install docker nvidia as root", lambda: d.install_docker(with_nvidia=True))
            step("gpu as root", lambda: d.enable_gpu_runtime())
        with mock.patch.object(deploy_module.sys, "platform", "linux"), \
                mock.patch.object(deploy_module.os, "geteuid", create=True, return_value=1000):
            step("install docker pkexec", lambda: d.install_docker())
            step("gpu pkexec", lambda: d.enable_gpu_runtime())
    calls.update(recorded)
    return recorded


class TestDockerArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docker_golden_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "empty")
        self.script = os.path.join(self.dir, "scripts", "server_ctl.py")
        self.docker_sh = os.path.join(self.dir, "scripts", "install-docker.sh")
        self.got = collect(deploy, self.tmp, {})

    def ctl(self, *args):
        return [("popen", [PY, self.script, "--json", *args], self.dir)]

    def assertCommands(self, name, expected):
        self.assertEqual(self.got[name], expected, name)

    def test_the_host_is_probed_before_a_clone_exists(self):
        self.assertCommands("status before install", [
            ("run", ["git", "--version"], None),
            ("run", ["docker", "--version"], None),
            ("run", ["docker", "info", "--format", "{{.ServerVersion}}"], None),
            ("run", ["docker", "compose", "version"], None),
            ("run", ["docker", "info", "--format", "{{json .Runtimes}}"], None),
            ("run", ["docker", "info", "--format", "{{json .DiscoveredDevices}}"], None),
        ])

    def test_clone(self):
        self.assertCommands("clone", [
            ("popen", ["git", "clone", "--branch", "main",
                       "https://example.org/VISOR-serve.git", self.dir], None)])

    def test_status(self):
        self.assertCommands("status", self.ctl("status", "--branch", "main"))
        self.assertCommands("status check remote",
                            self.ctl("status", "--branch", "main", "--check-remote"))

    def test_up(self):
        self.assertCommands("up", self.ctl("up"))
        self.assertCommands("up as the panel sends it",
                            self.ctl("up", "--port", "8000", "--bind", "127.0.0.1"))
        self.assertCommands("up port", self.ctl("up", "--port", "8123"))
        self.assertCommands("up everywhere", self.ctl("up", "--bind", ""))
        self.assertCommands("up force", self.ctl("up", "--force-recreate"))

    def test_update(self):
        self.assertCommands("update", self.ctl("update", "--branch", "main"))
        self.assertCommands("update as the panel sends it",
                            self.ctl("update", "--branch", "main", "--bind", "127.0.0.1"))
        self.assertCommands("update force bind", self.ctl(
            "update", "--branch", "main", "--bind", "100.94.102.62", "--force"))

    def test_down_logs_token_catalog(self):
        self.assertCommands("down", self.ctl("down"))
        self.assertCommands("logs", self.ctl("logs", "-n", "200"))
        self.assertCommands("logs 50", self.ctl("logs", "-n", "50"))
        self.assertCommands("token", self.ctl("token"))
        self.assertCommands("catalog", self.ctl("catalog"))

    def test_models(self):
        self.assertCommands("models", self.ctl("models", "--tool", "AMASSS", "--tool", "ALI"))
        self.assertCommands("models force", self.ctl("models", "--tool", "CNE", "--force"))

    def test_the_stop_on_exit_command(self):
        self.assertCommands("stop on exit", [("popen", [PY, self.script, "down"], self.dir)])

    def test_the_administrator_scripts(self):
        self.assertCommands("install docker as root",
                            [("popen", ["sh", self.docker_sh], None)])
        self.assertCommands("install docker nvidia as root",
                            [("popen", ["sh", self.docker_sh, "--nvidia"], None)])
        self.assertCommands("gpu as root", [("popen", ["sh", self.docker_sh, "--nvidia"], None)])
        self.assertCommands("install docker pkexec",
                            [("popen", ["pkexec", "sh", self.docker_sh], None)])
        self.assertCommands("gpu pkexec",
                            [("popen", ["pkexec", "sh", self.docker_sh, "--nvidia"], None)])

    def test_no_docker_command_ever_names_the_native_backend(self):
        for name, commands in self.got.items():
            for _kind, argv, _cwd in commands:
                for forbidden in ("--native", "--tools-dir", "--tls-cert", "--tls-key",
                                  "--tools-branch", "install-tools"):
                    self.assertNotIn(forbidden, argv, name)


if __name__ == "__main__":
    unittest.main()
