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
        self.assertIn("setup-tailscale.sh", message)
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


if __name__ == "__main__":
    unittest.main()
