"""The two backends behind their one interface.

    python3 -m unittest test_backends

Pure: no widget, no Slicer, no subprocess. What each backend verifies, plans,
hands the client and says is pinned here; the Docker argument vectors are
pinned byte for byte in test_docker_golden.py.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))
sys.path.insert(0, _HERE)

import fixtures  # noqa: E402
from SlicerCloudLib import backends, deploy, local_cert  # noqa: E402
from SlicerCloudLib.backends import DOCKER, NATIVE, DeploymentError  # noqa: E402

FREE = {"disk_free": 120 * fixtures.GIB, "port": 8000, "port_in_use": False}


class TestTheBackendIsChosenOnceFromThePlatform(unittest.TestCase):
    def chosen(self, system, machine, sysctl="0"):
        with mock.patch.object(deploy.sys, "platform", system), \
                mock.patch.object(deploy.platform, "machine", return_value=machine), \
                mock.patch.object(deploy.subprocess, "run", return_value=deploy.subprocess.CompletedProcess(
                    [], 0, sysctl + "\n", "")):
            return deploy.select_backend()

    def test_apple_silicon_is_native(self):
        self.assertIs(self.chosen("darwin", "arm64"), NATIVE)

    def test_rosetta_on_an_apple_cpu_is_native(self):
        self.assertIs(self.chosen("darwin", "x86_64", sysctl="1"), NATIVE)

    def test_an_intel_mac_is_docker(self):
        self.assertIs(self.chosen("darwin", "x86_64", sysctl="0"), DOCKER)

    def test_linux_and_windows_are_docker(self):
        self.assertIs(self.chosen("linux", "x86_64"), DOCKER)
        self.assertIs(self.chosen("linux", "aarch64"), DOCKER)
        self.assertIs(self.chosen("win32", "AMD64"), DOCKER)

    def test_an_explicit_choice_wins(self):
        self.assertIs(deploy.select_backend(True), NATIVE)
        self.assertIs(deploy.select_backend(False), DOCKER)

    def test_a_deployment_asks_once_and_keeps_the_answer(self):
        with mock.patch.object(deploy, "is_apple_silicon", return_value=True):
            d = deploy.LocalServerDeployment("/x")
        with mock.patch.object(deploy, "is_apple_silicon", side_effect=AssertionError("asked again")):
            self.assertIs(d.backend, NATIVE)
            self.assertTrue(d.native)
            d.backend.common_args(d)
            d.backend.checks(fixtures.native())


class TestArguments(unittest.TestCase):
    def deployment(self, native):
        return deploy.LocalServerDeployment("/srv/s", native=native, tools_dir="/srv/t")

    def test_docker_adds_nothing(self):
        d = self.deployment(False)
        self.assertEqual(DOCKER.common_args(d), [])
        self.assertEqual(DOCKER.common_args(d, "192.168.1.4"), [])
        self.assertEqual(DOCKER.start_args(d), [])
        self.assertEqual(DOCKER.update_args(d), [])

    def test_native_names_itself_and_the_tools(self):
        d = self.deployment(True)
        self.assertEqual(NATIVE.common_args(d), ["--native", "--tools-dir", "/srv/t"])

    def test_native_refuses_a_wider_bind(self):
        d = self.deployment(True)
        for bind in ("", "10.0.0.2"):
            with self.assertRaises(DeploymentError):
                NATIVE.common_args(d, bind)
        self.assertEqual(NATIVE.common_args(d, "127.0.0.1"), ["--native", "--tools-dir", "/srv/t"])

    def test_the_environment_names_the_backend_and_keeps_the_rest(self):
        self.assertEqual(DOCKER.subprocess_env({"A": "1"}), {"A": "1", "VISOR_BACKEND": "docker"})
        self.assertEqual(NATIVE.subprocess_env({"VISOR_BACKEND": "docker"}),
                         {"VISOR_BACKEND": "native"})

    def test_the_bind_policy(self):
        self.assertIsNone(DOCKER.fixed_bind)
        self.assertEqual(NATIVE.fixed_bind, "127.0.0.1")


class TestTheGridComesFromTheBackend(unittest.TestCase):
    def keys(self, backend, status, facts=None):
        return [check.key for check in backend.checks(status, facts)]

    def text(self, backend, status, facts=None):
        return " ".join(f"{c.label} {c.detail}" for c in backend.checks(status, facts)).lower()

    def test_the_native_grid(self):
        self.assertEqual(self.keys(NATIVE, fixtures.native(), FREE),
                         ["git", "uv", "gpu", "disk", "port", "clone", "health"])

    def test_the_native_grid_never_mentions_docker(self):
        for status in (fixtures.native(), fixtures.native_uninstalled(), fixtures.native_uninstalled(uv=None)):
            said = self.text(NATIVE, status, FREE)
            for word in ("docker", "compose", "container"):
                self.assertNotIn(word, said)

    def test_the_docker_grid(self):
        self.assertEqual(self.keys(DOCKER, fixtures.docker()),
                         ["git", "docker", "compose", "gpu", "clone", "container", "health"])

    def test_native_prerequisites_are_the_blocking_ones(self):
        blocking = [c.key for c in NATIVE.checks(fixtures.native(), FREE) if c.blocking]
        self.assertEqual(blocking, ["git", "uv"])

    def test_docker_prerequisites_are_the_blocking_ones(self):
        blocking = [c.key for c in DOCKER.checks(fixtures.docker()) if c.blocking]
        self.assertEqual(blocking, ["git", "docker", "compose"])

    def test_uv_missing_is_red_with_a_remedy(self):
        row = {c.key: c for c in NATIVE.checks(fixtures.native_uninstalled(uv=None), FREE)}["uv"]
        self.assertFalse(row.ok)
        self.assertEqual(row.remedy.key, "uv")
        self.assertIn("not installed", row.detail)

    def test_a_status_that_predates_the_uv_key_is_not_a_missing_uv(self):
        status = fixtures.native_uninstalled()
        del status["uv"]
        row = {c.key: c for c in NATIVE.checks(status, FREE)}["uv"]
        self.assertTrue(row.ok)

    def test_the_disk_row_says_what_is_free_and_what_setting_up_needs(self):
        row = {c.key: c for c in NATIVE.checks(fixtures.native_uninstalled(), FREE)}["disk"]
        self.assertTrue(row.ok)
        self.assertIn("120.0 GB free", row.detail)
        self.assertIn("about 15.0 GB", row.detail)
        tight = {c.key: c for c in NATIVE.checks(
            fixtures.native_uninstalled(), dict(FREE, disk_free=3 * fixtures.GIB))}["disk"]
        self.assertFalse(tight.ok)
        self.assertFalse(tight.blocking)

    def test_the_port_row(self):
        def row(**facts):
            return {c.key: c for c in NATIVE.checks(fixtures.native_uninstalled(), dict(FREE, **facts))}["port"]
        self.assertTrue(row().ok)
        self.assertIn("free", row().detail)
        self.assertFalse(row(port_in_use=True).ok)
        self.assertIn("another program", row(port_in_use=True).detail)
        running = {c.key: c for c in NATIVE.checks(
            fixtures.native(), dict(FREE, port_in_use=True, server_running=True))}["port"]
        self.assertTrue(running.ok)

    def test_the_gpu_rows(self):
        gpu = {c.key: c for c in NATIVE.checks(fixtures.native(), FREE)}["gpu"]
        self.assertTrue(gpu.ok)
        self.assertIn("Metal", gpu.detail)
        card = {c.key: c for c in DOCKER.checks(fixtures.docker(gpu={"nvidia_smi": True}))}["gpu"]
        self.assertFalse(card.ok)
        self.assertEqual(card.remedy.key, "gpu")
        self.assertFalse(card.blocking)

    def test_the_docker_remedies(self):
        outside = fixtures.docker(docker={"available": True, "daemon": False, "needs_group": True})
        self.assertEqual({c.key: c for c in DOCKER.checks(outside)}["docker"].remedy.key, "docker_group")
        absent = fixtures.docker(docker={"available": False, "daemon": False})
        self.assertEqual({c.key: c for c in DOCKER.checks(absent)}["docker"].remedy.key, "docker_install")

    def test_the_grid_cell_for_the_group_stays_one_line(self):
        outside = fixtures.docker(docker={"available": True, "daemon": False, "needs_group": True})
        self.assertNotIn("\n", {c.key: c for c in DOCKER.checks(outside)}["docker"].detail)


class FakeDeployment:
    def __init__(self, docker_usable=False):
        self.calls = []
        self.docker_usable = docker_usable

    def require_native_support(self, progress_cb=None):
        self.calls.append("support")

    def clone(self, progress_cb=None):
        self.calls.append("clone")

    def clone_tools(self, progress_cb=None):
        self.calls.append("tools")

    def install_tools(self, progress_cb=None):
        self.calls.append("environments")

    def up(self, progress_cb=None, port=None, bind=None):
        self.calls.append(("up", port, bind))
        return {"healthy": True}

    def status(self, progress_cb=None):
        self.calls.append("status")
        if self.docker_usable:
            return fixtures.docker_uninstalled()
        return fixtures.docker_uninstalled(docker={"available": False, "daemon": False, "error": "no docker"})

    install_docker_script = "/s/install-docker.sh"


class TestInstallPlan(unittest.TestCase):
    def keys(self, backend, status):
        return [step.key for step in backend.install_plan(status)]

    def run_plan(self, backend, status, docker_usable=True):
        d = FakeDeployment(docker_usable)
        result = None
        for step in backend.install_plan(status):
            result = step.run(d, None, {"port": 8000, "bind": "127.0.0.1"})
        return d.calls, result

    def test_native_from_nothing(self):
        self.assertEqual(self.keys(NATIVE, fixtures.native_uninstalled()),
                         ["support", "clone", "tools", "environments", "start"])
        calls, result = self.run_plan(NATIVE, fixtures.native_uninstalled())
        self.assertEqual(calls, ["support", "clone", "tools", "environments", ("up", 8000, "127.0.0.1")])
        self.assertEqual(result, {"healthy": True})

    def test_native_starting_an_installed_server_is_check_then_start(self):
        stopped = fixtures.native(container={"running": False})
        self.assertEqual(self.keys(NATIVE, stopped), ["support", "start"])

    def test_native_resumes_after_an_interrupted_install(self):
        halfway = fixtures.native(container={"running": False},
                                  tools={"venvs": {"AMASSS": True, "ALI_CBCT": False}})
        self.assertEqual(self.keys(NATIVE, halfway), ["support", "environments", "start"])
        no_tools = fixtures.native(container={"running": False}, tools={"present": False, "venvs": {}})
        self.assertEqual(self.keys(NATIVE, no_tools), ["support", "tools", "environments", "start"])

    def test_native_without_a_status_does_everything(self):
        self.assertEqual(self.keys(NATIVE, None),
                         ["support", "clone", "tools", "environments", "start"])

    def test_docker_is_clone_check_start(self):
        self.assertEqual(self.keys(DOCKER, fixtures.docker_uninstalled()), ["clone", "docker", "start"])
        calls, _ = self.run_plan(DOCKER, fixtures.docker_uninstalled())
        self.assertEqual(calls, ["clone", "status", ("up", 8000, "127.0.0.1")])

    def test_docker_refuses_before_starting_when_the_daemon_is_down(self):
        d = FakeDeployment()
        steps = DOCKER.install_plan(None)
        steps[0].run(d, None, {})
        with self.assertRaises(DeploymentError) as caught:
            steps[1].run(d, None, {})
        self.assertIn("sudo sh /s/install-docker.sh", str(caught.exception))
        self.assertNotIn(("up", None, None), d.calls)

    def test_every_step_has_a_label_and_a_weight(self):
        for backend in (DOCKER, NATIVE):
            for step in backend.install_plan(None):
                self.assertTrue(step.label)
                self.assertGreater(step.weight, 0)


class TestClientUrl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_pin_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(deploy.tls, "local_tls_dir", return_value=os.path.join(self.tmp, "tls"))
        patcher.start()
        self.addCleanup(patcher.stop)

    LOCAL_RESULT = {"url": "https://127.0.0.1:8000", "tls": {"enabled": True}}

    def test_docker_never_offers_the_local_address(self):
        local_cert.ensure_local_certificate(os.path.join(self.tmp, "tls"))
        self.assertIsNone(DOCKER.client_url(self.LOCAL_RESULT))

    def test_docker_offers_the_public_address(self):
        self.assertEqual(DOCKER.client_url({"public_url": "https://box.ts.net"}), "https://box.ts.net")
        self.assertIsNone(DOCKER.client_url({"public_url": "https://localhost:8000"}))
        self.assertIsNone(DOCKER.client_url({"public_url": "http://box.ts.net"}))

    def test_native_offers_the_pinned_address_once_the_certificate_exists(self):
        self.assertIsNone(NATIVE.client_url(self.LOCAL_RESULT))
        local_cert.ensure_local_certificate(os.path.join(self.tmp, "tls"))
        self.assertEqual(NATIVE.client_url(self.LOCAL_RESULT), "https://127.0.0.1:8000")

    def test_native_prefers_a_public_address(self):
        local_cert.ensure_local_certificate(os.path.join(self.tmp, "tls"))
        result = dict(self.LOCAL_RESULT, public_url="https://box.ts.net")
        self.assertEqual(NATIVE.client_url(result), "https://box.ts.net")

    def test_the_message_when_there_is_none(self):
        self.assertIn("serves HTTPS", NATIVE.no_url_message())
        self.assertIn("no public address", DOCKER.no_url_message())


class TestTexts(unittest.TestCase):
    def test_native_words_never_mention_docker(self):
        for name, value in vars(NATIVE.texts).items():
            for word in ("docker", "container"):
                self.assertNotIn(word, str(value).lower(), name)

    def test_both_backends_define_the_same_words(self):
        self.assertEqual(set(vars(DOCKER.texts)), set(vars(NATIVE.texts)))

    def test_every_hint_for_every_state_is_a_sentence_for_both(self):
        from SlicerCloudLib.state import State
        kinds = [value for name, value in vars(State).items() if not name.startswith("_")]
        statuses = {"docker": fixtures.docker(), "native": fixtures.native()}
        for backend, status in ((DOCKER, statuses["docker"]), (NATIVE, statuses["native"])):
            for kind in kinds:
                said = backend.hint(kind, status)
                self.assertTrue(said and said.endswith((".", ":")), (backend.name, kind, said))

    def test_native_hints_never_mention_docker(self):
        from SlicerCloudLib.state import State
        kinds = [value for name, value in vars(State).items() if not name.startswith("_")]
        for kind in kinds:
            self.assertNotIn("docker", NATIVE.hint(kind, fixtures.native()).lower(), kind)


class TestTheHostBeforeAClone(unittest.TestCase):
    class Host:
        def __init__(self, tools):
            self.tools, self.asked = tools, []

        def which(self, name):
            return "/bin/" + name if name in self.tools else None

        def version(self, command):
            self.asked.append(command)
            return "x 1", None

        def find_uv(self):
            return "/u/uv" if "uv" in self.tools else None

        def is_darwin(self):
            return True

    def test_native_asks_git_and_uv_and_never_docker(self):
        host = self.Host({"git", "uv", "docker"})
        probed = NATIVE.probe(host)
        self.assertEqual(host.asked, [["git", "--version"]])
        self.assertEqual(probed["uv"], "/u/uv")
        self.assertEqual(probed["gpu"]["gpu_access"], "mps")
        self.assertTrue(probed["native"])

    def test_native_without_uv_says_none(self):
        self.assertIsNone(NATIVE.probe(self.Host({"git"}))["uv"])

    def test_docker_asks_docker_not_uv(self):
        host = self.Host({"git", "docker"})
        probed = DOCKER.probe(host)
        self.assertNotIn("uv", probed)
        self.assertIn(["docker", "--version"], host.asked)
        self.assertFalse(probed["native"])


class TestEveryModuleOfTheLibraryIsShipped(unittest.TestCase):
    def test_cmake_lists_each_python_file_of_SlicerCloudLib(self):
        """A module missing from MODULE_PYTHON_SCRIPTS imports fine from the
        source tree, so every test passes, and fails only in an installed
        extension (this happened to local_cert.py)."""
        root = os.path.join(_HERE, "..", "..")
        with open(os.path.join(root, "CMakeLists.txt"), encoding="utf-8") as handle:
            listed = handle.read()
        library = os.path.join(root, "SlicerCloudLib")
        for name in sorted(os.listdir(library)):
            if name.endswith(".py"):
                self.assertIn(f"SlicerCloudLib/{name}", listed, f"{name} is not shipped")


if __name__ == "__main__":
    unittest.main()
