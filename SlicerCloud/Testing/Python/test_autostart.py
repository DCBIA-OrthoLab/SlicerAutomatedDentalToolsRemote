"""What SlicerCloud offers the tool panels: is a local server installed, and start it.

    python3 -m unittest test_autostart

The panels (ServerToolsCore) never import this package; SlicerCloud hands them
a provider at startup. Everything here is the part that can be tested without
Slicer: the provider reads nothing from Qt, the deployment is a fake, and the
rules are the ones the panels rely on: starting is idempotent, and it never
changes which server the modules use.
"""

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))
sys.path.insert(0, _HERE)

import fixtures  # noqa: E402
from SlicerCloudLib import autostart, backends  # noqa: E402


class FakeDeployment:
    def __init__(self, status, backend=backends.NATIVE, up=None, up_error=None):
        self.backend = backend
        self._status = status
        self._up = up if up is not None else {"token": "tok", "healthy": True}
        self._up_error = up_error
        self.calls = []

    def status(self, *a, **k):
        self.calls.append("status")
        return self._status

    def up(self, progress_cb=None, force_recreate=False, port=None, bind=None):
        self.calls.append(("up", port, bind, force_recreate))
        if self._up_error:
            raise self._up_error
        return self._up

    def token(self, progress_cb=None):
        self.calls.append("token")
        return "from-token"


class TestInstalled(unittest.TestCase):
    def test_a_complete_native_install_is_installed(self):
        local = autostart.LocalServer(FakeDeployment(fixtures.native()))
        self.assertTrue(local.installed())

    def test_nothing_cloned_is_not_installed(self):
        local = autostart.LocalServer(FakeDeployment(fixtures.native_uninstalled()))
        self.assertFalse(local.installed())

    def test_a_clone_without_the_tool_environments_is_not_installed(self):
        status = fixtures.native(tools={"present": True, "venvs": {"AMASSS": False}})
        self.assertFalse(autostart.LocalServer(FakeDeployment(status)).installed())

    def test_a_status_that_fails_is_not_an_install(self):
        class Broken(FakeDeployment):
            def status(self, *a, **k):
                raise backends.DeploymentError("no")
        self.assertFalse(autostart.LocalServer(Broken(None)).installed())

    def test_docker_clone_is_installed(self):
        local = autostart.LocalServer(FakeDeployment(fixtures.docker(), backends.DOCKER))
        self.assertTrue(local.installed())


class TestStart(unittest.TestCase):
    def setUp(self):
        # The pinned address needs the server's certificate on this disk.
        patcher = mock.patch.object(backends, "local_client_url", lambda status: (
            status.get("url") if (status.get("tls") or {}).get("enabled") else None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_runs_up_without_choosing_a_port_or_a_bind(self):
        """`up` keeps the port and the bind the install remembered: the panel
        must not undo a port chosen with 'Try another port'."""
        deployment = FakeDeployment(fixtures.native())
        result = autostart.LocalServer(deployment).start()
        self.assertEqual(deployment.calls[0], ("up", None, None, False))
        self.assertEqual(result["token"], "tok")
        self.assertEqual(result["url"], "https://127.0.0.1:8000")

    def test_an_already_running_server_is_a_success(self):
        up = {"token": "tok", "already_running": True, "restarted": False, "healthy": True}
        result = autostart.LocalServer(FakeDeployment(fixtures.native(), up=up)).start()
        self.assertTrue(result["already_running"])

    def test_the_token_comes_from_the_token_command_when_up_gave_none(self):
        deployment = FakeDeployment(fixtures.native(), up={"healthy": True})
        result = autostart.LocalServer(deployment).start()
        self.assertEqual(result["token"], "from-token")

    def test_a_failed_start_raises_what_the_panel_will_show(self):
        deployment = FakeDeployment(fixtures.native(), up_error=backends.DeploymentError("Port 8000 is taken"))
        with self.assertRaises(backends.DeploymentError) as caught:
            autostart.LocalServer(deployment).start()
        self.assertIn("Port 8000", str(caught.exception))

    def test_a_server_with_no_usable_address_carries_the_reason(self):
        status = fixtures.native(tls={"enabled": False})
        status["server"]["url"] = "https://127.0.0.1:8000"
        result = autostart.LocalServer(FakeDeployment(status)).start()
        self.assertIn("url", result)

    def test_start_never_asks_for_a_remote_server_or_replaces_one(self):
        deployment = FakeDeployment(fixtures.native())
        autostart.LocalServer(deployment).start()
        self.assertEqual([c for c in deployment.calls if isinstance(c, tuple)], [("up", None, None, False)])


class TestProvider(unittest.TestCase):
    def test_snapshot_builds_the_deployment_on_the_calling_thread(self):
        made = []

        def factory():
            made.append(1)
            return FakeDeployment(fixtures.native())

        provider = autostart.Provider(factory)
        local = provider.snapshot()
        self.assertEqual(made, [1])
        self.assertTrue(local.installed())
        self.assertEqual(made, [1], "the thread that uses it never rebuilds it")


if __name__ == "__main__":
    unittest.main()
