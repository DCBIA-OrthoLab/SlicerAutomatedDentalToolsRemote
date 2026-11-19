"""Plain unittest for SlicerCloudLib.deploy — no Slicer, no Qt, no network.

    python3 -m unittest test_deploy

`deploy.py` imports neither `slicer` nor `qt` (ARCHITECTURE.md's dependency
rule), which is what makes this runnable in ordinary CI. The subprocess
plumbing is exercised for real against a stand-in `server_ctl.py` written to a
temp directory: that is where the interesting failure modes live (a JSON result
on stdout while a live log streams on stderr, and the pipe deadlock that
naive two-pipe reading walks straight into), and none of them would show up
against a mocked `Popen`.
"""

import io
import os
import subprocess
import shutil
import stat
import time
import sys
import tempfile
import unittest
from unittest import mock

# The module directory, which is where SlicerCloudLib sits — the same import
# path Slicer itself provides once the module is installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# ServerToolsCore, for the ServerToolsCoreLib.tls that deploy.py shares with
# the client -- also on Slicer's own path once the extension is installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                                "ServerToolsCore"))

from SlicerCloudLib import deploy  # noqa: E402
from SlicerCloudLib.deploy import DeploymentError, LocalServerDeployment  # noqa: E402


def _write_fake_ctl(install_dir: str, body: str) -> str:
    """Install a stand-in scripts/server_ctl.py that runs `body`."""
    scripts = os.path.join(install_dir, "scripts")
    os.makedirs(scripts, exist_ok=True)
    path = os.path.join(scripts, "server_ctl.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("import json, sys\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class TempInstallDir(unittest.TestCase):
    def setUp(self):
        # These suites describe the Docker path. On an Apple-silicon runner the
        # deployment would otherwise choose the native backend by itself; the
        # native one has its own tests below, which ask for it explicitly.
        patcher = mock.patch.object(deploy, "is_apple_silicon", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="slicercloud_test_")
        self.install_dir = os.path.join(self.tmp, "SlicerCloudServer")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def deployment(self, **kwargs):
        return LocalServerDeployment(self.install_dir, python_executable=sys.executable, **kwargs)


class TestInstallDetection(TempInstallDir):
    def test_a_folder_without_the_control_script_is_not_an_install(self):
        """is_cloned is about server_ctl.py, not about the directory existing.

        A half-finished clone, or a folder the user made by hand, would
        otherwise turn every later call into an obscure "file not found"
        instead of "press Install".
        """
        os.makedirs(self.install_dir)
        self.assertFalse(self.deployment().is_cloned)

        _write_fake_ctl(self.install_dir, "print('{}')\n")
        self.assertTrue(self.deployment().is_cloned)

    def test_paths_hang_off_the_install_dir(self):
        deployment = self.deployment()
        self.assertEqual(deployment.data_dir, os.path.join(self.install_dir, "DATA"))
        self.assertEqual(
            deployment.install_docker_script,
            os.path.join(self.install_dir, "scripts", "install-docker.sh"),
        )


class TestRunCtl(TempInstallDir):
    def test_json_comes_back_from_stdout_while_the_log_streams_on_stderr(self):
        """The whole contract in one test: one parsed result, and every
        narration line delivered as it is written."""
        _write_fake_ctl(self.install_dir, (
            "for i in range(5):\n"
            "    print('step %d' % i, file=sys.stderr)\n"
            "print(json.dumps({'ok': True, 'args': sys.argv[1:]}))\n"
        ))
        lines = []
        result = self.deployment().run_ctl(["status"], progress_cb=lines.append)

        self.assertTrue(result["ok"])
        self.assertEqual(result["args"], ["--json", "status"])
        self.assertIn("step 0", lines)
        self.assertIn("step 4", lines)

    def test_a_large_log_does_not_deadlock(self):
        """stderr is drained by its own thread while stdout is read.

        Reading one pipe then the other hangs forever as soon as the writer
        fills the pipe nobody is reading — which is exactly what a fresh
        `docker compose up` does while pulling layers. 20k lines is far past
        any platform's pipe buffer.
        """
        _write_fake_ctl(self.install_dir, (
            "for i in range(20000):\n"
            "    print('layer %d downloading' % i, file=sys.stderr)\n"
            "print(json.dumps({'lines': 20000}))\n"
        ))
        seen = []
        result = self.deployment().run_ctl(["up"], progress_cb=seen.append)
        self.assertEqual(result["lines"], 20000)
        self.assertGreater(len(seen), 19000)

    def test_an_error_object_becomes_a_DeploymentError_with_the_server_message(self):
        """server_ctl.py writes its messages for the user; they must reach the
        error dialog verbatim rather than be replaced by a generic one."""
        _write_fake_ctl(self.install_dir, (
            "print(json.dumps({'error': 'The docker daemon refused this user.'}))\n"
            "sys.exit(1)\n"
        ))
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["up"])
        self.assertEqual(str(caught.exception), "The docker daemon refused this user.")

    def test_a_failure_carries_what_the_command_actually_said(self):
        """"failed (exit code 1)" and nothing else is what made a real failure
        on a fresh machine impossible to diagnose from the bug report. The tail
        of stderr is the diagnosis; it has to reach the dialog, not only the
        log pane the user may never open."""
        _write_fake_ctl(self.install_dir, (
            "print('ERROR: the docker daemon refused this user', file=sys.stderr)\n"
            "print('  hint: add yourself to the docker group', file=sys.stderr)\n"
            "sys.exit(1)\n"
        ))
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["status"])
        message = str(caught.exception)
        self.assertIn("exit code 1", message)
        self.assertIn("docker daemon refused this user", message)
        self.assertIn("add yourself to the docker group", message)

    def test_a_silent_failure_says_it_was_silent(self):
        """Distinguishable from "it said something you did not read"."""
        _write_fake_ctl(self.install_dir, "sys.exit(1)\n")
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["status"])
        self.assertIn("printed nothing at all", str(caught.exception))

    def test_only_the_TAIL_of_a_huge_log_travels(self):
        """`docker compose up` prints tens of thousands of lines; an error
        dialog must not try to show them all."""
        _write_fake_ctl(self.install_dir, (
            "for i in range(5000):\n"
            "    print('layer %d' % i, file=sys.stderr)\n"
            "sys.exit(1)\n"
        ))
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["up"])
        message = str(caught.exception)
        self.assertIn("layer 4999", message)
        self.assertNotIn("layer 100\n", message)
        self.assertLess(len(message.splitlines()), 20)

    def test_unparseable_output_is_reported_rather_than_swallowed(self):
        _write_fake_ctl(self.install_dir, "print('not json at all')\nsys.exit(3)\n")
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["status"])
        self.assertIn("exit code 3", str(caught.exception))

    def test_a_nonzero_exit_with_valid_json_still_fails(self):
        _write_fake_ctl(self.install_dir, "print(json.dumps({'ok': False}))\nsys.exit(2)\n")
        with self.assertRaises(DeploymentError):
            self.deployment().run_ctl(["down"])

    def test_calling_before_installing_says_so(self):
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().run_ctl(["status"])
        self.assertIn("not installed", str(caught.exception))


class TestCommandBuilding(TempInstallDir):
    """The flags each button sends. Cheap to get wrong, invisible when wrong."""

    def setUp(self):
        super().setUp()
        _write_fake_ctl(self.install_dir, "print(json.dumps({'args': sys.argv[1:]}))\n")

    def args_of(self, call):
        return call(self.deployment())["args"]

    def test_update_and_up_carry_their_flags(self):
        self.assertEqual(self.args_of(lambda d: d.up()), ["--json", "up"])
        self.assertEqual(self.args_of(lambda d: d.up(force_recreate=True)),
                         ["--json", "up", "--force-recreate"])
        self.assertEqual(self.args_of(lambda d: d.up(port=8123)),
                         ["--json", "up", "--port", "8123"])
        # deploy.DEFAULT_BRANCH, not the literal "main": which branch a
        # deployment ships is configuration, and a test that pins it fails the
        # moment someone legitimately retargets it.
        default = deploy.DEFAULT_BRANCH
        self.assertEqual(self.args_of(lambda d: d.update()),
                         ["--json", "update", "--branch", default])
        self.assertEqual(self.args_of(lambda d: d.update(force=True)),
                         ["--json", "update", "--branch", default, "--force"])

    def test_status_only_hits_the_network_when_asked(self):
        """The panel refreshes on every visit; a `git fetch` there would hang
        the module for 30s on a machine with no network."""
        default = deploy.DEFAULT_BRANCH
        self.assertEqual(self.args_of(lambda d: d.status()),
                         ["--json", "status", "--branch", default])
        self.assertEqual(self.args_of(lambda d: d.status(check_remote=True)),
                         ["--json", "status", "--branch", default, "--check-remote"])

    def test_the_configured_branch_travels_on_every_status_and_update(self):
        """A clone is created ONCE. Without the branch on every later call, a
        deployment repointed at another branch would keep following the old one
        in silence — the change to the Branch field would simply do nothing."""
        for call in (lambda d: d.status(), lambda d: d.update()):
            args = call(LocalServerDeployment(
                self.install_dir, branch="a-very-specific-branch",
                python_executable=sys.executable))["args"]
            self.assertIn("--branch", args)
            self.assertEqual(args[args.index("--branch") + 1], "a-very-specific-branch")

    def test_every_selected_tool_is_named(self):
        self.assertEqual(
            self.args_of(lambda d: d.download_data(["AMASSS", "ALI"])),
            ["--json", "models", "--tool", "AMASSS", "--tool", "ALI"],
        )

    def test_an_empty_selection_is_refused_rather_than_meaning_everything(self):
        """"Download selected" with nothing ticked must not start a 29 GB run."""
        for empty in ([], [""], None):
            with self.assertRaises(DeploymentError):
                self.deployment().download_data(empty or [])

    def test_status_adds_the_cloned_flag_the_panel_switches_on(self):
        self.assertTrue(self.deployment().status()["cloned"])


class TestStatusBeforeInstalling(TempInstallDir):
    def test_it_answers_the_same_shape_without_a_clone(self):
        """The panel reads one dict whether or not anything is installed —
        otherwise its very first render, the one a new user sees, is the one
        code path nothing exercises."""
        status = self.deployment().status()
        self.assertFalse(status["cloned"])
        for key in ("git", "docker", "compose", "gpu", "clone", "container", "server", "env"):
            self.assertIn(key, status)
        self.assertFalse(status["server"]["healthy"])
        self.assertEqual(status["repo_root"], self.install_dir)

    def test_the_gpu_is_probed_rather_than_assumed_absent(self):
        """The pre-install screen is the ONLY one a new user sees before
        pressing Install, and it used to pin nvidia_runtime to False — so a
        machine with a perfectly working card was told it had none, and the
        panel offered to repair something that was not broken.

        Probed for real here, so this asserts the shape and the honesty of the
        answer rather than a value: what it must never do again is hardcode.
        """
        status = self.deployment().status()
        gpu = status["gpu"]
        self.assertIn("nvidia_runtime", gpu)
        self.assertIn("nvidia_smi", gpu)
        self.assertIn("gpu_access", gpu)
        # The two fields have to agree: a mechanism was named if and only if
        # docker can reach a card.
        self.assertEqual(bool(gpu["nvidia_runtime"]), gpu["gpu_access"] is not None)
        self.assertIn(gpu["gpu_access"], (None, "runtime", "cdi"))

    def test_a_cdi_device_counts_as_a_reachable_gpu(self):
        """The bug this came from: docker >= 25 reaches the card through a CDI
        device with no 'nvidia' runtime registered anywhere, which the old check
        read as 'no GPU' and silently answered with the CPU service."""
        self.assertTrue(deploy._has_nvidia_cdi_device(
            '[{"Source":"cdi","ID":"nvidia.com/gpu=all"}]'))
        self.assertFalse(deploy._has_nvidia_cdi_device(
            '[{"Source":"cdi","ID":"othervendor.com/fpga=all"}]'))

    def test_an_older_docker_reads_as_no_cdi_not_as_an_error(self):
        """`.DiscoveredDevices` does not exist before docker 28, where an
        unknown field fails the whole template. That must mean 'no CDI', never
        'could not check' — otherwise every older host becomes an error."""
        for absent in (None, "", "null", "<no value>", "[]", '{"Source":"cdi"}'):
            self.assertFalse(deploy._has_nvidia_cdi_device(absent), absent)


class TestClone(TempInstallDir):
    def test_a_non_empty_destination_is_refused_with_advice(self):
        os.makedirs(self.install_dir)
        with open(os.path.join(self.install_dir, "something.txt"), "w", encoding="utf-8") as handle:
            handle.write("mine")
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().clone()
        self.assertIn("not empty", str(caught.exception))

    def test_a_foreign_git_clone_is_named_as_such(self):
        """A clone of the wrong repository fails differently from an untouched
        folder, and the fix is different too."""
        os.makedirs(os.path.join(self.install_dir, ".git"))
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().clone()
        self.assertIn("server_ctl.py", str(caught.exception))

    def test_an_existing_install_is_reused_silently(self):
        _write_fake_ctl(self.install_dir, "print('{}')\n")
        messages = []
        self.deployment().clone(progress_cb=messages.append)
        self.assertTrue(any("Already installed" in message for message in messages))


class TestStopDetached(TempInstallDir):
    """The application-quit path. It must not delay Slicer's exit, and it must
    outlive the process that asked for it."""

    def test_it_returns_immediately_and_the_stop_still_happens(self):
        """A real `docker compose stop` on this image measures 10.5 s (uvicorn
        --reload ignores SIGTERM, so compose waits out the grace period). Every
        one of those seconds would be Slicer refusing to close, so the call
        detaches. The stand-in below sleeps, then writes a marker: the caller
        must come back long before the marker exists."""
        marker = os.path.join(self.tmp, "stopped.marker")
        _write_fake_ctl(self.install_dir, (
            "import time\n"
            "time.sleep(1.5)\n"
            f"open({marker!r}, 'w').write('done')\n"
            "print('{}')\n"
        ))
        started = time.monotonic()
        launched = self.deployment().stop_detached()
        elapsed = time.monotonic() - started

        self.assertTrue(launched)
        self.assertLess(elapsed, 1.0, "stop_detached blocked the caller")
        self.assertFalse(os.path.exists(marker), "it waited for the stop to finish")

        deadline = time.monotonic() + 15
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(marker), "the detached stop never ran")

    def test_it_is_a_no_op_when_nothing_is_installed(self):
        """Slicer quits on machines that never had a deployment. That must not
        spawn anything, and must not raise inside an aboutToQuit handler."""
        self.assertFalse(self.deployment().stop_detached())


class TestSubprocessEnvironment(TempInstallDir):
    """Slicer's launcher exports PYTHONHOME/PYTHONPATH, so a subprocess running
    the system python3 starts against SLICER's stdlib and dies on "SRE module
    mismatch" before any of our code runs. Every spawn must use the
    launcher-free environment."""

    def tearDown(self):
        deploy.set_subprocess_env(None)
        super().tearDown()

    def test_the_configured_environment_reaches_the_subprocess(self):
        _write_fake_ctl(self.install_dir, (
            "import os\n"
            "print(json.dumps({'marker': os.environ.get('SLICERCLOUD_TEST_MARKER'),\n"
            "                  'pythonhome': os.environ.get('PYTHONHOME')}))\n"
        ))
        deploy.set_subprocess_env({"PATH": os.environ.get("PATH", ""),
                                   "SLICERCLOUD_TEST_MARKER": "clean"})
        result = self.deployment().run_ctl(["status"])
        self.assertEqual(result["marker"], "clean")
        self.assertIsNone(result["pythonhome"], "the polluting variable survived")

    def test_none_restores_plain_inheritance(self):
        _write_fake_ctl(self.install_dir, (
            "import os\n"
            "print(json.dumps({'inherited': os.environ.get('SLICERCLOUD_TEST_MARKER')}))\n"
        ))
        os.environ["SLICERCLOUD_TEST_MARKER"] = "inherited"
        try:
            deploy.set_subprocess_env(None)
            self.assertEqual(self.deployment().run_ctl(["status"])["inherited"], "inherited")
        finally:
            os.environ.pop("SLICERCLOUD_TEST_MARKER", None)


class TestListTools(TempInstallDir):
    """Read from the deployment's own URL, and never raise for a server that is
    simply not there — "unknown" is a state the panel renders, an exception is
    not."""

    def test_an_unreachable_server_is_None_rather_than_an_error(self):
        self.assertIsNone(self.deployment().list_tools("https://127.0.0.1:1", timeout=1))

    def test_the_default_is_https(self):
        self.assertTrue(deploy.DEFAULT_SERVER_URL.startswith("https://"))

    def test_a_remote_http_url_is_never_asked(self):
        with mock.patch.object(deploy.tls, "_opener") as urlopen:
            self.assertIsNone(self.deployment().list_tools("http://192.0.2.1:8000", timeout=1))
        urlopen.assert_not_called()

    def test_a_loopback_http_url_is_never_asked_either(self):
        with mock.patch.object(deploy.tls, "_opener") as urlopen:
            for url in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
                self.assertIsNone(self.deployment().list_tools(url, timeout=1))
        urlopen.assert_not_called()

    def test_no_url_is_unknown_rather_than_localhost(self):
        with mock.patch.object(deploy.tls, "_opener") as urlopen:
            self.assertIsNone(self.deployment().list_tools(None, timeout=1))
        urlopen.assert_not_called()

    def test_the_public_authorities_verify_it(self):
        seen = {}

        class _Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _Opener:
            def __init__(self, context):
                seen["context"] = context

            def open(self, url, timeout):
                seen["url"] = url
                return _Answer(b'[{"name": "AMASSS"}]')

        with mock.patch.object(deploy.tls, "_opener", _Opener):
            names = self.deployment().list_tools("https://visor.example.ts.net/")
        self.assertEqual(names, ["AMASSS"])
        self.assertEqual(seen["url"], "https://visor.example.ts.net/tools")
        self.assertTrue(seen["context"].check_hostname)
        self.assertEqual(seen["context"].verify_mode, deploy.tls.ssl.CERT_REQUIRED)


    def test_both_shapes_GET_tools_could_answer_with(self):
        for payload, expected in (
            ([{"name": "b"}, {"name": "a"}], ["a", "b"]),   # the list the server sends
            ({"b": {}, "a": {}}, ["a", "b"]),               # a name-keyed mapping
            ([{"name": "a"}, {"no_name": 1}], ["a"]),       # a malformed entry is skipped
            ("nonsense", None),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(deploy.tool_names(payload), expected)


class TestClientUrl(unittest.TestCase):
    """Which URL the client is handed: the public one, or none at all."""

    def test_the_public_url_is_handed_over(self):
        # `up` reports it at the top level, `status` under `server`; a tailnet
        # address and a reverse proxy's own domain are equally valid.
        self.assertEqual(deploy.client_url({"url": "https://localhost:8000",
                                            "public_url": "https://v.example.ts.net/"}),
                         "https://v.example.ts.net")
        self.assertEqual(deploy.client_url({"server": {"url": "https://localhost:8000",
                                                       "public_url": "https://tools.example.org"}}),
                         "https://tools.example.org")

    def test_an_empty_public_url_means_none(self):
        self.assertIsNone(deploy.client_url({"url": "https://localhost:8000", "public_url": ""}))
        self.assertIsNone(deploy.client_url({"server": {"public_url": "", "url": "https://localhost:8000"}}))
        self.assertIsNone(deploy.client_url({"tailnet_url": "https://v.example.ts.net"}))

    def test_the_local_listener_is_never_handed_over(self):
        for url in ("https://localhost:8000", "https://127.0.0.1:8000", "https://[::1]:8000",
                    "http://localhost:8000", "http://v.example.ts.net"):
            with self.subTest(url=url):
                self.assertIsNone(deploy.client_url({"url": url}))
                self.assertIsNone(deploy.client_url({"public_url": url}))
        self.assertIsNone(deploy.client_url({}))
        self.assertIsNone(deploy.client_url(None))

    def test_the_message_names_every_way_to_get_a_public_address(self):
        message = deploy.NO_PUBLIC_URL_MESSAGE
        self.assertIn("tailscale serve", message)
        self.assertNotIn("setup-tailscale", message)
        self.assertIn("PUBLIC_URL", message)
        self.assertIn("reverse proxy", message)


class TestDockerInstall(TempInstallDir):
    """The one action that runs as root. Every guard on it is worth pinning."""

    def test_it_refuses_before_the_clone_exists(self):
        """The installer is a file in the repository; there is nothing to run
        until it has been cloned."""
        with self.assertRaises(DeploymentError) as caught:
            self.deployment().install_docker()
        self.assertIn("install-docker.sh", str(caught.exception))

    def test_it_is_only_offered_where_it_can_actually_be_delivered(self):
        """macOS and Windows need a GUI installer, so a button promising to do
        it there would be a lie. can_install_docker says so up front."""
        original = deploy.sys.platform
        try:
            deploy.sys.platform = "darwin"
            self.assertFalse(self.deployment().can_install_docker())
        finally:
            deploy.sys.platform = original

    def test_the_non_linux_path_names_docker_desktop(self):
        os.makedirs(os.path.join(self.install_dir, "scripts"))
        with open(os.path.join(self.install_dir, "scripts", "install-docker.sh"), "w",
                  encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        original = deploy.sys.platform
        try:
            deploy.sys.platform = "win32"
            with self.assertRaises(DeploymentError) as caught:
                self.deployment().install_docker()
        finally:
            deploy.sys.platform = original
        self.assertIn("Docker Desktop", str(caught.exception))


class TestFindPython(unittest.TestCase):
    def test_it_returns_a_working_python3(self):
        interpreter = deploy.find_python()
        self.assertTrue(os.path.basename(interpreter))

    def test_it_never_returns_a_non_python_sys_executable(self):
        """Inside Slicer, sys.executable can be the Slicer application. Handing
        that to subprocess launches a second Slicer instead of running a
        script, with no error message that says so."""
        original = sys.executable
        try:
            sys.executable = os.path.join(os.sep, "opt", "slicer", "Slicer")
            interpreter = deploy.find_python()
            self.assertNotEqual(interpreter, sys.executable)
        finally:
            sys.executable = original


# --- the native backend (Apple-silicon Mac) ---------------------------------
#
# Nothing below starts a server or a build: commands are captured at `run_ctl`
# / `_spawn`, which is where the argv leaves this module.

class TestAppleSiliconDetection(unittest.TestCase):
    def test_native_is_for_apple_silicon_only(self):
        self.assertTrue(deploy.is_apple_silicon("darwin", "arm64"))
        self.assertFalse(deploy.is_apple_silicon("darwin", "x86_64", sysctl="0"))
        self.assertFalse(deploy.is_apple_silicon("linux", "aarch64"))
        self.assertFalse(deploy.is_apple_silicon("linux", "x86_64"))
        self.assertFalse(deploy.is_apple_silicon("win32", "AMD64"))

    def test_a_rosetta_process_on_an_apple_cpu_still_counts(self):
        """The stable Slicer for macOS is x86_64: platform.machine() lies there,
        `sysctl hw.optional.arm64` does not."""
        self.assertTrue(deploy.is_apple_silicon("darwin", "x86_64", sysctl="1\n"))

    def test_an_intel_mac_stays_on_docker(self):
        """No native backend for Intel: the deployment is Docker, its commands
        carry no --native, and no certificate is made."""
        with mock.patch.object(deploy.sys, "platform", "darwin"), \
                mock.patch.object(deploy.platform, "machine", return_value="x86_64"), \
                mock.patch.object(deploy.subprocess, "run",
                                  return_value=deploy.subprocess.CompletedProcess([], 0, "0\n", "")):
            self.assertFalse(deploy.is_apple_silicon())
            self.assertFalse(deploy.prefer_native())
            d = LocalServerDeployment("/x")
        self.assertFalse(d.native)
        self.assertEqual(d.backend.common_args(d), [])
        self.assertEqual(d.backend.start_args(d), [])

    def test_the_default_follows_the_machine(self):
        with mock.patch.object(deploy, "is_apple_silicon", return_value=True):
            self.assertTrue(LocalServerDeployment("/x").native)
            self.assertFalse(LocalServerDeployment("/x", native=False).native)
        with mock.patch.object(deploy, "is_apple_silicon", return_value=False):
            self.assertFalse(LocalServerDeployment("/x").native)
            self.assertTrue(LocalServerDeployment("/x", native=True).native)


class NativeCase(TempInstallDir):
    def setUp(self):
        super().setUp()
        _write_fake_ctl(self.install_dir, "print('{}')\n")
        self.pin_dir = os.path.join(self.tmp, "pin")
        patcher = mock.patch.object(deploy.tls, "local_tls_dir", return_value=self.pin_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = os.path.join(self.tmp, "SADT-VISOR")
        self.captured = []

    def native(self, **kwargs):
        return self.deployment(native=True, tools_dir=self.tools, **kwargs)

    def capture(self, deployment):
        def fake(args, progress_cb=None):
            self.captured.append(list(args))
            return {"url": "https://127.0.0.1:8000"}
        return mock.patch.object(deployment, "run_ctl", side_effect=fake)


class TestNativeCommands(NativeCase):
    def test_up_names_the_backend_the_tools_and_the_certificate(self):
        d = self.native()
        with self.capture(d):
            d.up(port=8123, bind=deploy.BIND_LOCALHOST)
        args = self.captured[0]
        cert = os.path.join(self.pin_dir, "server.crt")
        key = os.path.join(self.pin_dir, "server.key")
        self.assertEqual(args, ["up", "--native", "--tools-dir", self.tools,
                                "--tls-cert", cert, "--tls-key", key,
                                "--port", "8123", "--bind", "127.0.0.1"])

    def test_the_certificate_exists_when_up_is_called(self):
        d = self.native()
        with self.capture(d):
            d.up()
        self.assertTrue(os.path.isfile(os.path.join(self.pin_dir, "server.crt")))
        self.assertTrue(os.path.isfile(os.path.join(self.pin_dir, "server.key")))
        self.assertEqual(deploy.tls.pinned_certificate("127.0.0.1"),
                         os.path.join(self.pin_dir, "server.crt"))

    def test_update_carries_the_certificate_too(self):
        d = self.native()
        with self.capture(d), self.publishes():
            d.update(force=True)
        args = self.captured[0]
        self.assertEqual(args[:4], ["update", "--native", "--tools-dir", self.tools])
        self.assertIn("--tls-cert", args)
        self.assertIn("--tls-key", args)
        self.assertEqual(args[args.index("--branch") + 1], deploy.DEFAULT_BRANCH)
        self.assertEqual(args[-1], "--force")

    def publishes(self, supported=True):
        return mock.patch.object(deploy.LocalServerDeployment, "_ctl_publishes", return_value=supported)

    def test_update_names_the_tools_branch(self):
        d = self.native(tools_branch="update")
        with self.capture(d), self.publishes():
            d.update(force=True)
        args = self.captured[0]
        i = args.index("--tools-branch")
        self.assertEqual(args[i:i + 2], ["--tools-branch", "update"])
        self.assertNotIn("--tools-url", args)
        self.assertEqual(args[-1], "--force")

    def test_update_names_the_tools_url_only_when_it_is_not_the_default(self):
        url = "https://github.com/someone/SADT-VISOR.git"
        d = self.native(tools_branch="dev", tools_repo_url=url)
        with self.capture(d), self.publishes():
            d.update()
        args = self.captured[0]
        self.assertEqual(args[args.index("--tools-branch"):][:4], ["--tools-branch", "dev", "--tools-url", url])
        d = self.native()
        with self.capture(d), self.publishes():
            d.update()
        self.assertEqual(self.captured[1][self.captured[1].index("--tools-branch") + 1], deploy.DEFAULT_TOOLS_BRANCH)
        self.assertNotIn("--tools-url", self.captured[1])

    def test_an_older_server_is_not_sent_options_it_does_not_know(self):
        d = self.native()
        with self.capture(d), self.publishes(False):
            d.update()
        self.assertNotIn("--tools-branch", self.captured[0])
        self.assertNotIn("--tools-url", self.captured[0])

    def test_an_older_server_refuses_a_chosen_branch_in_plain_words(self):
        for kwargs in ({"tools_branch": "update"},
                       {"tools_repo_url": "https://example.org/x/SADT-VISOR.git"}):
            d = self.native(**kwargs)
            with self.capture(d), self.publishes(False), self.assertRaises(DeploymentError) as caught:
                d.update()
            self.assertIn("too old", str(caught.exception))
            self.assertIn("not touched", str(caught.exception))
            self.assertEqual(self.captured, [])

    def test_the_help_probe_reads_what_the_installed_server_publishes(self):
        with_option = "import sys\nprint('usage: update [--tools-branch B]' if 'update' in sys.argv else '')\n"
        _write_fake_ctl(self.install_dir, with_option)
        self.assertTrue(self.native()._ctl_publishes("--tools-branch"))
        _write_fake_ctl(self.install_dir, "print('usage: update [--force]')\n")
        self.assertFalse(self.native()._ctl_publishes("--tools-branch"))
        _write_fake_ctl(self.install_dir, "import sys\nsys.exit(2)\n")
        self.assertFalse(self.native()._ctl_publishes("--tools-branch"))

    def test_a_refused_switch_is_shown_as_is_and_says_the_old_server_runs(self):
        refusal = ("Tools checkout /x/SADT-VISOR: it is on 'main' and 'update' was asked for, but the "
                   "tree has uncommitted changes. Commit or discard them, then update again.")
        d = self.native(tools_branch="update")
        with self.publishes(), mock.patch.object(d, "run_ctl", side_effect=DeploymentError(refusal)), \
                self.assertRaises(DeploymentError) as caught:
            d.update()
        message = str(caught.exception)
        self.assertTrue(message.startswith(refusal))
        self.assertIn("still running", message)
        self.assertNotIn("nothing is serving", message)

    def test_a_failed_relaunch_says_the_old_server_is_stopped(self):
        stopped = ("The old native server was stopped, and the new one did not start (X: y). "
                   "Nothing is serving now.")
        d = self.native()
        with self.publishes(), mock.patch.object(d, "run_ctl", side_effect=DeploymentError(stopped)), \
                self.assertRaises(DeploymentError) as caught:
            d.update()
        message = str(caught.exception)
        self.assertTrue(message.startswith(stopped))
        self.assertIn("was stopped and the new one is not running", message)
        self.assertNotIn("still running", message)

    def test_other_update_errors_are_passed_on_untouched(self):
        d = self.native()
        with self.publishes(), mock.patch.object(d, "run_ctl", side_effect=DeploymentError("boom")), \
                self.assertRaises(DeploymentError) as caught:
            d.update()
        self.assertEqual(str(caught.exception), "boom")

    def test_docker_update_argv_is_what_it_was_before(self):
        d = self.deployment(native=False, tools_branch="update",
                            tools_repo_url="https://example.org/x/SADT-VISOR.git")
        with self.capture(d), mock.patch.object(deploy.subprocess, "run") as run:
            d.update()
            d.update(force=True, bind=deploy.BIND_LOCALHOST)
        run.assert_not_called()
        self.assertEqual(self.captured[0], ["update", "--branch", deploy.DEFAULT_BRANCH])
        self.assertEqual(self.captured[1], ["update", "--branch", deploy.DEFAULT_BRANCH,
                                            "--bind", "127.0.0.1", "--force"])

    def test_install_tools_is_native_only_and_names_each_tool(self):
        d = self.native()
        with self.capture(d):
            d.install_tools()
            d.install_tools(tools=["AMASSS", "ALI_CBCT"], force=True)
        self.assertEqual(self.captured[0], ["install-tools", "--native", "--tools-dir", self.tools])
        self.assertEqual(self.captured[1], ["install-tools", "--native", "--tools-dir", self.tools,
                                            "--tool", "AMASSS", "--tool", "ALI_CBCT", "--force"])
        with self.assertRaises(DeploymentError):
            self.deployment(native=False).install_tools()

    def test_the_rest_of_the_commands_say_native_too(self):
        d = self.native()
        with self.capture(d):
            d.status(check_remote=True)
            d.down()
            d.logs(50)
        base = ["--native", "--tools-dir", self.tools]
        self.assertEqual(self.captured[0], ["status"] + base + ["--branch", deploy.DEFAULT_BRANCH,
                                                                 "--check-remote"])
        self.assertEqual(self.captured[1], ["down"] + base)
        self.assertEqual(self.captured[2], ["logs"] + base + ["-n", "50"])

    def test_up_and_update_never_reach_for_the_certificate_in_docker_mode(self):
        d = self.deployment(native=False)
        with self.capture(d):
            d.up(port=8123)
            d.update()
        self.assertEqual(self.captured[0], ["up", "--port", "8123"])
        self.assertEqual(self.captured[1], ["update", "--branch", deploy.DEFAULT_BRANCH])
        self.assertFalse(os.path.exists(self.pin_dir))

    def test_a_wider_bind_is_refused_before_anything_starts(self):
        d = self.native()
        for bind in (deploy.BIND_EVERYWHERE, "192.168.1.10"):
            with self.capture(d), self.assertRaises(DeploymentError):
                d.up(bind=bind)
            with self.capture(d), self.assertRaises(DeploymentError):
                d.update(bind=bind)
        self.assertEqual(self.captured, [])
        self.assertFalse(os.path.exists(self.pin_dir))

    def test_a_certificate_failure_is_a_DeploymentError(self):
        d = self.native()
        boom = deploy.local_cert.CertificateError("no openssl")
        with mock.patch.object(deploy.local_cert, "ensure_local_certificate", side_effect=boom), \
                self.capture(d), self.assertRaises(DeploymentError) as caught:
            d.up()
        self.assertIn("no openssl", str(caught.exception))
        self.assertEqual(self.captured, [])

    def test_the_stop_on_quit_is_native_too(self):
        d = self.native()
        with mock.patch.object(deploy.subprocess, "Popen") as popen:
            self.assertTrue(d.stop_detached())
        command = popen.call_args[0][0]
        self.assertEqual(command[2:], ["down", "--native", "--tools-dir", self.tools])


class TestNativeToolsCheckout(NativeCase):
    def test_the_tools_folder_is_beside_the_install_folder(self):
        d = self.deployment(native=True)
        self.assertEqual(d.tools_dir, os.path.join(self.tmp, "SADT-VISOR"))
        self.assertFalse(d.tools_cloned)

    def test_it_is_cloned_with_the_configured_branch(self):
        d = self.native(tools_branch="feat/mps-devices", tools_repo_url="https://example.org/t.git")
        with mock.patch.object(d, "_spawn", return_value=(1, "")) as spawn, \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/git"), \
                self.assertRaises(DeploymentError):
            d.clone_tools()
        self.assertEqual(spawn.call_args[0][0], ["git", "clone", "--branch", "feat/mps-devices",
                                                 "https://example.org/t.git", self.tools])

    def test_an_existing_checkout_is_reused(self):
        os.makedirs(os.path.join(self.tools, "tools"))
        d = self.native()
        with mock.patch.object(d, "_spawn") as spawn:
            d.clone_tools()
        spawn.assert_not_called()

    def test_a_foreign_folder_is_not_cloned_over(self):
        os.makedirs(self.tools)
        open(os.path.join(self.tools, "mine.txt"), "w").close()
        d = self.native()
        with mock.patch.object(d, "_spawn") as spawn, \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/git"), \
                self.assertRaises(DeploymentError):
            d.clone_tools()
        spawn.assert_not_called()

    def test_docker_mode_clones_nothing(self):
        d = self.deployment(native=False)
        with mock.patch.object(d, "_spawn") as spawn:
            d.clone_tools()
        spawn.assert_not_called()


class TestNativeStatusBeforeInstalling(NativeCase):
    def test_docker_is_never_asked(self):
        shutil.rmtree(os.path.join(self.install_dir, "scripts"))     # nothing installed yet
        d = self.native()
        asked = []

        def fake_run(command, **_kwargs):
            asked.append(command[0])
            return deploy.subprocess.CompletedProcess(command, 0, "git version 2.50\n", "")

        with mock.patch.object(deploy.subprocess, "run", fake_run), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/x"):
            status = d.status()
        self.assertNotIn("docker", asked)
        self.assertTrue(status["native"])
        self.assertFalse(status["cloned"])
        self.assertFalse(status["docker"]["available"])


class TestNativeClientUrl(NativeCase):
    def setUp(self):
        super().setUp()
        self.reported = {"url": "https://127.0.0.1:8123", "token": "t", "healthy": True,
                         "tls": {"enabled": True, "cert": os.path.join(self.pin_dir, "server.crt")}}

    def pin(self):
        deploy.local_cert.ensure_local_certificate(self.pin_dir)

    def test_the_pinned_local_address_is_handed_over(self):
        self.pin()
        self.assertEqual(deploy.client_url(self.reported), "https://127.0.0.1:8123")
        # `status` reports the same thing under `server`
        status = {"server": {"url": "http://127.0.0.1:8123", "healthy": True},
                  "tls": {"enabled": True}}
        self.assertEqual(deploy.client_url(status), "https://127.0.0.1:8123")
        # a stand-in answering from [::1] still gets OUR host; a name is not accepted
        self.assertEqual(deploy.client_url(dict(self.reported, url="https://[::1]:9000")),
                         "https://127.0.0.1:9000")
        self.assertIsNone(deploy.client_url(dict(self.reported, url="https://localhost:9000")))

    def test_without_the_server_saying_it_serves_tls_nothing_is_handed_over(self):
        self.pin()
        for tls_field in (None, {}, {"enabled": False}, "yes", []):
            with self.subTest(tls=tls_field):
                result = dict(self.reported, tls=tls_field)
                self.assertIsNone(deploy.client_url(result))
        result = dict(self.reported)
        del result["tls"]
        self.assertIsNone(deploy.client_url(result))

    def test_without_a_pinned_certificate_nothing_is_handed_over(self):
        self.assertIsNone(deploy.client_url(self.reported))

    def test_only_this_machine_qualifies(self):
        self.pin()
        for url in ("https://10.0.0.5:8123", "https://server.example.org:8123", "https://127.0.0.2:8123",
                    "https://127.0.0.1", "", None):
            with self.subTest(url=url):
                self.assertIsNone(deploy.client_url(dict(self.reported, url=url)))

    def test_a_server_serving_some_other_certificate_is_not_trusted_to_say_so(self):
        self.pin()
        other = dict(self.reported, tls={"enabled": True, "cert": "/tmp/elsewhere.crt"})
        self.assertIsNone(deploy.client_url(other))

    def test_a_public_url_still_wins_and_docker_results_are_unchanged(self):
        self.pin()
        self.assertEqual(deploy.client_url(dict(self.reported, public_url="https://v.example.ts.net")),
                         "https://v.example.ts.net")
        self.assertIsNone(deploy.client_url({"url": "https://localhost:8000"}))

    def test_the_message_for_a_native_server_that_does_not_report_tls(self):
        self.assertIn("Update server", deploy.NO_LOCAL_TLS_MESSAGE)
        self.assertNotIn("setup-tailscale", deploy.NO_LOCAL_TLS_MESSAGE)


if __name__ == "__main__":
    unittest.main()


def _git(cwd, *args):
    done = subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return done.stdout.strip()


def _commit_file(repo, relpath, text="x\n"):
    full = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", "c " + relpath)


class GitCase(NativeCase):
    """A real origin with an old `main` (no native code) and a `mac` branch with it,
    and a clone of it as the panel would have made."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.install_dir)
        self.origin = os.path.join(self.tmp, "origin")
        os.makedirs(self.origin)
        _git(self.origin, "init", "-q", "-b", "main")
        _commit_file(self.origin, "scripts/server_ctl.py", "print('{}')\n")
        _git(self.origin, "checkout", "-q", "-b", "mac")
        _commit_file(self.origin, "scripts/server_ctl_native.py")
        _git(self.origin, "checkout", "-q", "main")
        _git(self.tmp, "clone", "-q", "--branch", "main", self.origin, self.install_dir)

    def head(self):
        return _git(self.install_dir, "rev-parse", "--abbrev-ref", "HEAD"), _git(self.install_dir, "rev-parse", "HEAD")


class TestNativeSupportCheck(GitCase):
    def test_a_branch_with_native_support_passes_without_touching_the_clone(self):
        d = self.native(branch="mac", repo_url=self.origin)
        before = self.head()
        d.require_native_support()
        self.assertEqual(self.head(), before)
        self.assertEqual(_git(self.install_dir, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "scripts", "server_ctl_native.py")))

    def test_a_branch_without_it_is_refused_before_anything_changes(self):
        _git(self.origin, "branch", "old", "main")
        d = self.native(branch="old", repo_url=self.origin)
        before = self.head()
        with self.assertRaises(DeploymentError) as caught:
            d.require_native_support()
        message = str(caught.exception)
        self.assertIn("'old'", message)
        self.assertIn("no native", message)
        self.assertIn("Branch", message)
        self.assertIn("Repository URL", message)
        self.assertEqual(self.head(), before)
        self.assertEqual(_git(self.install_dir, "remote", "get-url", "origin"), self.origin)

    def test_a_clone_already_on_the_branch_is_read_in_place(self):
        d = self.native(branch="main", repo_url=self.origin)
        with mock.patch.object(deploy.subprocess, "run", wraps=deploy.subprocess.run) as run:
            with self.assertRaises(DeploymentError):
                d.require_native_support()
        self.assertFalse(any("fetch" in call.args[0] for call in run.call_args_list))
        _git(self.install_dir, "merge", "-q", "origin/main")
        _git(self.install_dir, "fetch", "-q", "origin", "mac")
        _git(self.install_dir, "merge", "-q", "FETCH_HEAD")
        self.native(branch="main", repo_url=self.origin).require_native_support()

    def test_docker_never_looks(self):
        d = self.deployment(native=False, branch="old")
        with mock.patch.object(deploy.subprocess, "run") as run:
            d.require_native_support()
        run.assert_not_called()

    def test_an_unreachable_origin_does_not_block(self):
        _git(self.install_dir, "remote", "set-url", "origin", os.path.join(self.tmp, "gone"))
        messages = []
        d = self.native(branch="mac")
        d.require_native_support(progress_cb=messages.append)
        self.assertTrue(any("could not check" in m.lower() for m in messages))

    def test_nothing_to_check_before_the_first_clone(self):
        shutil.rmtree(self.install_dir)
        self.native(branch="old").require_native_support()

    def test_a_fresh_clone_of_a_branch_without_it_is_refused(self):
        shutil.rmtree(self.install_dir)
        d = self.native(branch="main", repo_url=self.origin)
        with self.assertRaises(DeploymentError) as caught:
            d.clone()
        self.assertIn("no native", str(caught.exception))


class TestRawArgparseIsExplained(NativeCase):
    def failing(self, stderr_text):
        _write_fake_ctl(self.install_dir, f"sys.stderr.write({stderr_text!r} + '\\n')\nsys.exit(2)\n")
        return self.native(branch="main")

    def test_invalid_choice_install_tools(self):
        d = self.failing("server_ctl.py: error: argument command: invalid choice: 'install-tools' "
                         "(choose from 'up', 'down')")
        with self.assertRaises(DeploymentError) as caught:
            d.install_tools()
        message = str(caught.exception)
        self.assertIn("no native", message)
        self.assertIn("'main'", message)
        self.assertIn("invalid choice: 'install-tools'", message)

    def test_unrecognized_native_arguments(self):
        d = self.failing("server_ctl.py: error: unrecognized arguments: --native --tools-dir /x")
        with self.assertRaises(DeploymentError) as caught:
            d.up()
        self.assertIn("no native", str(caught.exception))
        self.assertIn("unrecognized arguments", str(caught.exception))

    def test_docker_keeps_the_raw_message(self):
        _write_fake_ctl(self.install_dir, "sys.stderr.write('error: unrecognized arguments: --native\\n')\nsys.exit(2)\n")
        d = self.deployment(native=False)
        with self.assertRaises(DeploymentError) as caught:
            d.run_ctl(["up"])
        self.assertNotIn("no native", str(caught.exception))
        self.assertIn("unrecognized arguments", str(caught.exception))


class TestBranchSwitches(GitCase):
    def test_a_clone_on_the_requested_branch_has_nothing_to_switch(self):
        self.assertEqual(self.native(branch="main").pending_branch_switches(), [])

    def test_the_server_clone_on_another_branch_is_named(self):
        (switch,) = self.native(branch="mac").pending_branch_switches()
        self.assertEqual((switch.current, switch.wanted, switch.dirty), ("main", "mac", False))
        self.assertEqual(switch.path, self.install_dir)
        self.assertIn("server", switch.what)

    def test_a_dirty_tree_is_flagged(self):
        with open(os.path.join(self.install_dir, "scripts", "server_ctl.py"), "a") as handle:
            handle.write("# local edit\n")
        (switch,) = self.native(branch="mac").pending_branch_switches()
        self.assertTrue(switch.dirty)

    def test_untracked_files_do_not_count_as_dirty(self):
        open(os.path.join(self.install_dir, "scratch.txt"), "w").close()
        (switch,) = self.native(branch="mac").pending_branch_switches()
        self.assertFalse(switch.dirty)

    def test_the_tools_clone_counts_in_native_mode_only(self):
        shutil.copytree(self.origin, self.tools)
        _git(self.tools, "checkout", "-q", "mac")
        os.makedirs(os.path.join(self.tools, "tools"))
        switches = self.native(branch="main", tools_branch="main").pending_branch_switches()
        self.assertEqual([(s.current, s.wanted) for s in switches], [("mac", "main")])
        self.assertIn("tools", switches[0].what)
        self.assertEqual(self.deployment(native=False, branch="main", tools_branch="main",
                                         ).pending_branch_switches(), [])

    def test_a_missing_clone_has_nothing_to_switch(self):
        shutil.rmtree(self.install_dir)
        self.assertEqual(self.native(branch="mac").pending_branch_switches(), [])


class TestTheBackendIsNamedToTheServer(TempInstallDir):
    """server_ctl.py picks the native backend by itself on every darwin, while
    this client keeps an Intel Mac on Docker. The client's choice is therefore
    stated in the environment of every server_ctl.py it starts
    ($VISOR_BACKEND, which the script already reads), so both always agree."""

    def tearDown(self):
        deploy.set_subprocess_env(None)
        super().tearDown()

    def backend_seen_by_ctl(self, **kwargs):
        _write_fake_ctl(self.install_dir, (
            "import os\n"
            "print(json.dumps({'backend': os.environ.get('VISOR_BACKEND')}))\n"))
        return self.deployment(**kwargs).run_ctl(["status"])["backend"]

    def test_a_docker_deployment_says_docker(self):
        self.assertEqual(self.backend_seen_by_ctl(native=False), "docker")

    def test_a_native_deployment_says_native(self):
        self.assertEqual(self.backend_seen_by_ctl(native=True), "native")

    def test_it_overrides_whatever_the_environment_already_had(self):
        deploy.set_subprocess_env({"PATH": os.environ.get("PATH", ""), "VISOR_BACKEND": "native"})
        self.assertEqual(self.backend_seen_by_ctl(native=False), "docker")

    def test_an_intel_mac_is_docker_all_the_way_to_the_server(self):
        with mock.patch.object(deploy.sys, "platform", "darwin"), \
                mock.patch.object(deploy.platform, "machine", return_value="x86_64"), \
                mock.patch.object(deploy, "is_apple_silicon", return_value=False):
            self.assertEqual(self.backend_seen_by_ctl(), "docker")

    def test_the_configured_environment_is_kept(self):
        _write_fake_ctl(self.install_dir, (
            "import os\n"
            "print(json.dumps({'marker': os.environ.get('SLICERCLOUD_TEST_MARKER')}))\n"))
        deploy.set_subprocess_env({"PATH": os.environ.get("PATH", ""),
                                   "SLICERCLOUD_TEST_MARKER": "kept"})
        self.assertEqual(self.deployment(native=False).run_ctl(["status"])["marker"], "kept")

    def test_the_stop_on_exit_command_carries_it_too(self):
        _write_fake_ctl(self.install_dir, "print('{}')\n")
        with mock.patch.object(deploy.subprocess, "Popen") as popen:
            self.deployment(native=False).stop_detached()
        self.assertEqual(popen.call_args[1]["env"]["VISOR_BACKEND"], "docker")


class TestNativePrerequisitesBeforeTheClone(NativeCase):
    def probe(self, uv):
        shutil.rmtree(os.path.join(self.install_dir, "scripts"), ignore_errors=True)
        with mock.patch.object(deploy.subprocess, "run", return_value=deploy.subprocess.CompletedProcess(
                [], 0, "git version 2.50\n", "")), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(deploy, "find_uv", return_value=uv):
            return self.native().status()

    def test_uv_is_reported_before_anything_is_cloned(self):
        self.assertIsNone(self.probe(None)["uv"])
        self.assertEqual(self.probe("/opt/homebrew/bin/uv")["uv"], "/opt/homebrew/bin/uv")

    def test_find_uv_looks_where_a_gui_application_does_not_have_on_its_path(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".local", "bin"))
        uv = os.path.join(home, ".local", "bin", "uv")
        with open(uv, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(uv, 0o755)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}, clear=False), \
                mock.patch.object(deploy.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"UV": ""}):
            self.assertEqual(deploy.find_uv(), uv)


class TestDescribeUpdate(unittest.TestCase):
    def test_what_each_backend_did(self):
        d = deploy.describe_update
        self.assertEqual(d({"pulled": True, "clone": {"commit": "abc"}}), "Updated to abc and relaunched.")
        self.assertEqual(d({"recreated": True}), "Already up to date; the container was relaunched.")
        self.assertEqual(d({}), "Already up to date and running, nothing to do.")
        self.assertEqual(d({"tools_pulled": True, "restarted": True}),
                         "Tools updated; the server was relaunched.")
        self.assertEqual(d({"pulled": True, "tools_pulled": True, "restarted": True,
                            "clone": {"commit": "abc"}}),
                         "Updated to abc and the tools; the server was relaunched.")
        self.assertEqual(d({"restarted": True}), "Already up to date; the server was relaunched.")
        self.assertEqual(d({"venvs": [{"action": "built"}], "restarted": True}),
                         "1 tool environment(s) rebuilt; the server was relaunched.")
