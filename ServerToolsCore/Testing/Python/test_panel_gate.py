"""The card that replaces a tool's form when there is no server to talk to.

One place in `ServerToolWidgetBase`, so one test covers what all sixteen
modules do; `TestEveryToolModuleGetsTheCard` checks that none of them opted out
by overriding `setup`.

Stand-ins only: Qt is a set of stubs and BackgroundJob is delivered on command.
That proves what the card says, shows, and calls. What a card looks like in a
Slicer window is not proven here.
"""

import ast
import glob
import os
import sys
import threading
import types
import unittest
from unittest import mock

_HERE = os.path.abspath(os.path.dirname(__file__))
_CORE = os.path.abspath(os.path.join(_HERE, "..", ".."))
_ROOT = os.path.abspath(os.path.join(_CORE, ".."))
sys.path.insert(0, _HERE)
sys.path.insert(0, _CORE)

import test_hosted_test_files as fixtures  # noqa: E402,F401 - installs the stubs

from ServerToolsCoreLib import base_widget, gate  # noqa: E402
from ServerToolsCoreLib.base_widget import ServerToolWidgetBase  # noqa: E402

qt = fixtures.qt

PLACEHOLDER = "https://set-your-server.invalid"
REMOTE = "https://lab-server.example.org"
LOCAL = "https://127.0.0.1:8000"


class _Job:
    started = []

    def __init__(self, target, on_success=None, on_error=None, on_progress=None, cancel_event=None):
        self.target, self.on_success, self.on_error = target, on_success, on_error
        self.cancel_event = cancel_event or threading.Event()

    def start(self):
        _Job.started.append(self)

    def cancel(self):
        self.cancel_event.set()

    def finish(self):
        """Run the target (as the worker thread would), then deliver here."""
        try:
            result = self.target(lambda *a, **k: None)
        except Exception as exc:  # noqa: BLE001
            self.on_error(exc)
        else:
            self.on_success(result)


class _Client:
    def __init__(self, url, healthy=False):
        self.server_url, self.token, self.timeout = url, "tok", 600
        self.healthy = healthy
        self.health_calls = 0
        self.configured = []

    def health(self):
        self.health_calls += 1
        return self.healthy

    def configure(self, **kwargs):
        self.configured.append(kwargs)
        if "server_url" in kwargs:
            self.server_url = kwargs["server_url"]


class _Local(gate.LocalServer):
    def __init__(self, installed=True, error=None):
        self._installed, self._error = installed, error
        self.starts = 0

    def installed(self):
        return self._installed

    def start(self):
        self.starts += 1
        if self._error:
            raise self._error
        return {"url": LOCAL, "token": "fresh"}


class _Provider:
    def __init__(self, local):
        self.local = local
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return self.local


class GateCase(unittest.TestCase):
    def setUp(self):
        _Job.started = []
        self.addCleanup(setattr, base_widget, "BackgroundJob", base_widget.BackgroundJob)
        base_widget.BackgroundJob = _Job
        self.addCleanup(gate.register_provider, None)
        gate.register_provider(None)
        self.selected = []
        util = sys.modules["slicer"].util
        self.addCleanup(lambda: setattr(util, "selectModule", getattr(self, "_old_select", None)))
        self._old_select = getattr(util, "selectModule", None)
        util.selectModule = self.selected.append
        slicer = sys.modules["slicer"]
        self.addCleanup(lambda old=getattr(slicer, "modules", None): setattr(slicer, "modules", old))
        slicer.modules = types.SimpleNamespace(slicercloud=object())
        self.saved = []
        patcher = mock.patch.object(base_widget, "_save_server_settings",
                                    lambda url, token, timeout: self.saved.append((url, token, timeout)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def panel(self, url, healthy=False, local=None):
        panel = ServerToolWidgetBase.__new__(ServerToolWidgetBase)
        panel.TOOL_NAME = "ALI_CBCT"
        panel.client = _Client(url, healthy)
        panel.uiWidget = qt.QWidget()
        panel._statusJob = None
        panel._schemaError = None
        panel._statusBadge = qt.QLabel()
        panel.rebuilds = []
        panel._buildForm = lambda force_refresh=False: panel.rebuilds.append(force_refresh)
        panel._gateCard = panel._buildGateCard()
        if local is not None:
            gate.register_provider(_Provider(local))
        return panel

    def visible(self, panel, *names):
        return {name: getattr(panel, name).isVisible() for name in names}

    def run_jobs(self):
        while _Job.started:
            _Job.started.pop(0).finish()


class TestNoServerSet(GateCase):
    def test_the_card_replaces_the_form_and_offers_two_actions(self):
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        self.assertTrue(panel._gateCard.isVisible())
        self.assertFalse(panel.uiWidget.isVisible())
        self.assertEqual(panel._gateTitle.text, "No server is set")
        self.assertEqual(self.visible(panel, "_gateConnect", "_gateCompute", "_gateStart"),
                         {"_gateConnect": True, "_gateCompute": True, "_gateStart": False})
        self.assertEqual(panel._gateConnect.text, "Connect to a server")
        self.assertEqual(panel._gateCompute.text, "Compute on this computer")

    def test_without_the_cloud_module_only_connect_is_offered(self):
        sys.modules["slicer"].modules = types.SimpleNamespace()
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        self.assertEqual(self.visible(panel, "_gateConnect", "_gateCompute"),
                         {"_gateConnect": True, "_gateCompute": False})

    def test_no_request_is_made_for_the_placeholder_host(self):
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        self.run_jobs()
        self.assertEqual(panel.client.health_calls, 0)

    def test_the_two_buttons_lead_to_the_right_modules(self):
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        panel._gateConnect.click()
        panel._gateCompute.click()
        self.assertEqual(self.selected, ["ServerToolsSettings", "SlicerCloud"])

    def test_the_card_hides_again_once_a_server_is_set(self):
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        panel.client.server_url = REMOTE
        panel._applyGate()
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())

    def test_the_text_is_plain_and_says_what_to_do(self):
        panel = self.panel(PLACEHOLDER)
        panel._applyGate()
        body = panel._gateBody.text
        for jargon in ("DNS", "TLS", "https", ".invalid", "token", "URL", "Docker"):
            self.assertNotIn(jargon, body)
        self.assertTrue(panel._gateBody.wordWrap)


class TestAConfiguredServerIsUntouched(GateCase):
    def test_a_remote_server_shows_the_form_and_starts_nothing(self):
        local = _Local()
        panel = self.panel(REMOTE, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)          # unreachable, and it is the person's choice
        self.run_jobs()
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())
        self.assertEqual((local.starts, gate.provider().snapshots), (0, 0))

    def test_a_server_that_answers_starts_nothing(self):
        local = _Local()
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(True)
        self.run_jobs()
        self.assertEqual(local.starts, 0)


class TestTheLocalServerStoppedWithSlicer(GateCase):
    def test_a_stopped_local_server_starts_by_itself_and_the_form_comes_back(self):
        local = _Local()
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.assertEqual(panel._gateTitle.text, "Starting VISOR Local")
        self.assertTrue(panel._gateCard.isVisible())
        self.assertFalse(panel.uiWidget.isVisible())
        self.assertEqual(self.visible(panel, "_gateConnect", "_gateCompute", "_gateStart"),
                         {"_gateConnect": False, "_gateCompute": False, "_gateStart": False})
        self.run_jobs()
        self.assertEqual(local.starts, 1)
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())
        self.assertEqual(panel.rebuilds, [True], "the form is rebuilt from the server that is now up")
        self.assertEqual(self.saved, [], "the address did not change, nothing is saved")

    def test_nothing_is_installed_so_the_existing_screen_stays(self):
        local = _Local(installed=False)
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertEqual(local.starts, 0)
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())

    def test_without_the_cloud_module_nothing_is_attempted(self):
        panel = self.panel(LOCAL)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())

    def test_a_failed_start_says_why_and_offers_a_way_out(self):
        local = _Local(error=RuntimeError("Port 8000 is taken"))
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertEqual(panel._gateTitle.text, "VISOR Local did not start")
        self.assertIn("Port 8000 is taken", panel._gateBody.text)
        self.assertTrue(panel._gateCard.isVisible())
        self.assertEqual(self.visible(panel, "_gateOpenLocal", "_gateTryAgain"),
                         {"_gateOpenLocal": True, "_gateTryAgain": True})
        panel._gateOpenLocal.click()
        self.assertEqual(self.selected, ["SlicerCloud"])

    def test_try_again_starts_it_again(self):
        local = _Local(error=RuntimeError("no"))
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        local._error = None
        panel._gateTryAgain.click()
        self.run_jobs()
        self.assertEqual(local.starts, 2)
        self.assertTrue(panel.uiWidget.isVisible())

    def test_a_failure_is_not_retried_on_every_health_check_of_the_visit(self):
        local = _Local(error=RuntimeError("no"))
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertEqual(local.starts, 1)

    def test_a_second_health_check_while_starting_does_not_start_twice(self):
        local = _Local()
        panel = self.panel(LOCAL, local=local)
        panel._applyGate()
        panel._onStatusChecked(False)
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertEqual(local.starts, 1)


class TestTheInstallQuestionNeverBlocksTheStart(GateCase):
    def test_a_probe_still_running_does_not_swallow_the_automatic_start(self):
        """Found in a real Slicer: the card asks "is it installed" when the panel
        is built, and that answer can still be pending when the first health
        check of a configured local server comes back."""
        local = _Local()
        panel = self.panel(PLACEHOLDER, local=local)
        panel._applyGate()                     # the probe is now in flight
        panel.client.server_url = LOCAL
        panel._applyGate()
        panel._onStatusChecked(False)
        self.run_jobs()
        self.assertEqual(local.starts, 1)


class TestNoServerButAnInstall(GateCase):
    def test_the_card_offers_start_when_an_install_is_found(self):
        local = _Local(installed=True)
        panel = self.panel(PLACEHOLDER, local=local)
        panel._applyGate()
        self.assertFalse(panel._gateStart.isVisible(), "not before the install is known")
        self.run_jobs()
        self.assertTrue(panel._gateStart.isVisible())
        self.assertEqual(local.starts, 0, "nothing starts without a click: no server was chosen")
        self.assertIn("already set up", panel._gateBody.text)

    def test_no_install_no_start_button(self):
        panel = self.panel(PLACEHOLDER, local=_Local(installed=False))
        panel._applyGate()
        self.run_jobs()
        self.assertFalse(panel._gateStart.isVisible())

    def test_start_points_the_client_at_the_local_server_and_shows_the_form(self):
        local = _Local()
        panel = self.panel(PLACEHOLDER, local=local)
        panel._applyGate()
        self.run_jobs()
        panel._gateStart.click()
        self.run_jobs()
        self.assertEqual(local.starts, 1)
        self.assertEqual(panel.client.configured, [{"server_url": LOCAL, "token": "fresh"}])
        self.assertEqual(self.saved, [(LOCAL, "fresh", 600)])
        self.assertFalse(panel._gateCard.isVisible())
        self.assertTrue(panel.uiWidget.isVisible())
        self.assertEqual(panel.rebuilds, [True])


    def test_a_server_with_no_usable_address_is_reported_not_guessed(self):
        local = _Local()
        local.start = lambda: {"url": None, "token": "t", "no_url_message": "No address was published."}
        panel = self.panel(PLACEHOLDER, local=local)
        panel._applyGate()
        self.run_jobs()
        panel._gateStart.click()
        self.run_jobs()
        self.assertEqual(panel.client.configured, [])
        self.assertEqual(self.saved, [])
        self.assertEqual(panel._gateTitle.text, "VISOR Local did not start")
        self.assertIn("No address was published.", panel._gateBody.text)


    def test_try_again_after_a_failed_start_from_the_card_starts_again(self):
        local = _Local(error=RuntimeError("no"))
        panel = self.panel(PLACEHOLDER, local=local)
        panel._applyGate()
        self.run_jobs()
        panel._gateStart.click()
        self.run_jobs()
        self.assertEqual(panel._gateTitle.text, "VISOR Local did not start")
        local._error = None
        panel._gateTryAgain.click()
        self.run_jobs()
        self.assertEqual(local.starts, 2)
        self.assertEqual(panel.client.server_url, LOCAL)


class TestEveryToolModuleGetsTheCard(unittest.TestCase):
    def test_sixteen_modules_inherit_the_base_and_none_overrides_the_lifecycle(self):
        found = []
        for path in sorted(glob.glob(os.path.join(_ROOT, "*", "*.py"))):
            module = os.path.basename(os.path.dirname(path))
            if os.path.basename(path)[:-3] != module:
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            try:
                tree = ast.parse(text)
            except SyntaxError:
                # A module written for Slicer's Python 3.12 (nested quotes in an
                # f-string) that an older interpreter cannot parse: not one of
                # the tool modules that inherit the base, which are all plain.
                self.assertNotIn("(ServerToolWidgetBase)", text, module)
                continue
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                if "ServerToolWidgetBase" not in bases:
                    continue
                found.append(module)
                overridden = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                self.assertFalse(overridden & {"setup", "enter", "_onStatusChecked", "_buildForm"},
                                 f"{module} overrides the lifecycle the card hangs on")
        self.assertEqual(len(found), 16, found)


if __name__ == "__main__":
    unittest.main()
