"""What the Slicer Cloud panel does when it opens — with qt/ctk/slicer stubbed.

    python3 -m unittest test_panel

This exists because of a bug a user hit on the very first screen: they opened
the module, picked an install folder, and **could not press "Install and
start"**. Two causes, both invisible from the code alone:

* assigning `currentPath` in `_loadSettings` emits `currentPathChanged`, so a
  refresh started from inside `setup()` — and `setup()`'s own refresh then
  found one running and popped a modal on every module open;
* that refresh disabled every action button while it ran, and it runs on
  `setup()` and on every `enter()`.

Neither is reachable by `test_deploy.py` (no widgets) and neither would show up
in review. They show up here.

`BackgroundJob` is replaced by a synchronous stand-in so the job lifecycle
(`_setBusy` → task → `on_success`) runs deterministically, and by a *deferred*
one where a test needs to look at the panel mid-job.
"""

import os
import sys
import types
import getpass
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))                      # SlicerCloud/
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..",
                                "ServerToolsCore", "Testing", "Python"))  # qt_stubs
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))

import qt_stubs  # noqa: E402


# --- the handful of pieces qt_stubs does not carry -------------------------

def _extend_stubs():
    """Add what this panel uses and formgen never did."""
    def setEnabled(self, enabled):
        self._enabled = bool(enabled)

    def isEnabled(self):
        return getattr(self, "_enabled", True)

    qt_stubs.QObject.setEnabled = setEnabled
    qt_stubs.QObject.isEnabled = isEnabled

    # Nested layouts and the item API `_renderCatalog` uses to clear its rows.
    # formgen only ever added widgets, so qt_stubs stopped there.
    def addLayout(self, layout, stretch=0):
        self.widgets.append(layout)

    def addStretch(self, _stretch=0):
        pass

    def count(self):
        return len(self.widgets)

    def takeAt(self, index):
        item = self.widgets.pop(index)
        return types.SimpleNamespace(
            layout=lambda: item if isinstance(item, qt_stubs.QLayout) else None,
            widget=lambda: None if isinstance(item, qt_stubs.QLayout) else item,
        )

    for name, function in (("addLayout", addLayout), ("addStretch", addStretch),
                           ("count", count), ("takeAt", takeAt)):
        setattr(qt_stubs.QLayout, name, function)

    def setParent(self, _parent):
        pass

    def deleteLater(self):
        pass

    qt_stubs.QObject.setParent = setParent
    qt_stubs.QObject.deleteLater = deleteLater
    qt_stubs.QLayout.deleteLater = deleteLater
    qt_stubs.QObject.setColumnStretch = lambda self, *_a: None
    qt_stubs.QGridLayout.setColumnStretch = lambda self, *_a: None

    # qt_stubs exposes `text` as a plain attribute; the panel also calls the
    # setter form on buttons and labels.
    def setText(self, text):
        self.text = text

    def setWordWrap(self, _value):
        pass

    qt_stubs.QPushButton.setText = setText
    qt_stubs.QLabel.setText = setText
    qt_stubs.QLabel.setWordWrap = setWordWrap
    qt_stubs.QCheckBox.setToolTip = qt_stubs.QObject.setToolTip

    # PythonQt exposes QCheckBox's state as the `checked` property, which is
    # how the panel reads and writes it.
    def _checked_get(self):
        return getattr(self, "_checked", False)

    def _checked_set(self, value):
        self._checked = bool(value)
        self.toggled.emit(self._checked)

    qt_stubs.QCheckBox.checked = property(_checked_get, _checked_set)
    qt_stubs.QCheckBox.setChecked = lambda self, value: setattr(self, "checked", value)
    qt_stubs.QCheckBox.isChecked = lambda self: self.checked

    class QTextEdit(qt_stubs.QObject):
        NoWrap = 0

        def __init__(self):
            qt_stubs.QObject.__init__(self)
            self.lines = []

        def setReadOnly(self, _value):
            pass

        def setLineWrapMode(self, _mode):
            pass

        # A PROPERTY, not a method — PythonQt collapses QTextEdit's
        # document()/setDocument() pair into an attribute, and modelling it as
        # a method is what let `self.logView.document()` ship. It raised
        # "'QTextDocument' object is not callable" inside setup() on a fresh
        # Slicer, leaving the whole panel half-built.
        @property
        def document(self):
            return types.SimpleNamespace(setMaximumBlockCount=lambda _n: None)

        def append(self, text):
            self.lines.append(text)

        def ensureCursorVisible(self):
            pass

        def clear(self):
            self.lines = []

    class QSettings:
        """One process-wide dict, like the real per-user store."""

        store = {}

        def value(self, key, default=None):
            return QSettings.store.get(key, default)

        def setValue(self, key, value):
            QSettings.store[key] = value

        def contains(self, key):
            return key in QSettings.store

        def remove(self, key):
            QSettings.store.pop(key, None)

        def sync(self):
            pass

    qt_stubs.QTextEdit = QTextEdit
    qt_stubs.QSettings = QSettings
    return QSettings


_QSettings = _extend_stubs()
qt, ctk = qt_stubs.install()
qt.QTextEdit = qt_stubs.QTextEdit
qt.QSettings = qt_stubs.QSettings

# `slicer` is a bare module from qt_stubs.install(); the panel needs a few
# entry points from it, and the dialogs must be observable rather than shown.
slicer = sys.modules["slicer"]
DIALOGS = {"error": [], "warning": [], "info": [], "confirm": []}


def _reset_dialogs():
    for value in DIALOGS.values():
        del value[:]


slicer.util = types.SimpleNamespace(
    errorDisplay=lambda text, **_k: DIALOGS["error"].append(text),
    warningDisplay=lambda text, **_k: DIALOGS["warning"].append(text),
    infoDisplay=lambda text, **_k: DIALOGS["info"].append(text),
    showStatusMessage=lambda *_a, **_k: None,
    confirmOkCancelDisplay=lambda text, *_a, **_k: (DIALOGS["confirm"].append(text) or True),
)
slicer.app = types.SimpleNamespace(connect=lambda *_a, **_k: None,
                                   palette=lambda: qt.QPalette())


class _ScriptedLoadableModule:
    def __init__(self, parent):
        self.parent = parent


class _ScriptedLoadableModuleWidget:
    def __init__(self, parent=None):
        self.parent = parent
        self.layout = qt.QVBoxLayout()

    def setup(self):
        pass


slicer.ScriptedLoadableModule = types.ModuleType("slicer.ScriptedLoadableModule")
slicer.ScriptedLoadableModule.ScriptedLoadableModule = _ScriptedLoadableModule
slicer.ScriptedLoadableModule.ScriptedLoadableModuleWidget = _ScriptedLoadableModuleWidget
sys.modules["slicer.ScriptedLoadableModule"] = slicer.ScriptedLoadableModule
slicer.i18n = types.ModuleType("slicer.i18n")
slicer.i18n.tr = lambda text: text
sys.modules["slicer.i18n"] = slicer.i18n

# ServerToolsCoreLib.design/worker are real; only the HTTP client and the
# settings writer are stubbed, since neither belongs in a widget test.
#
# The stub is a FALLBACK, not the default. `setdefault` used to install an empty
# module unconditionally — `requests` is not imported before this line, so it was
# never already in sys.modules and the real library was always shadowed. That
# broke the whole suite at import: ServerToolsCoreLib.transfer annotates a
# parameter `requests.Session`, evaluated when the module is read, so every test
# here died on AttributeError before a single one ran. Nothing in a widget test
# makes an HTTP call, so either module does; only the names touched at import
# time have to exist.
try:
    import requests  # noqa: F401
except ImportError:
    _requests = types.ModuleType("requests")
    _requests.Session = object
    _requests.RequestException = Exception
    _requests.adapters = types.SimpleNamespace(HTTPAdapter=object)
    sys.modules["requests"] = _requests

import SlicerCloud  # noqa: E402
from SlicerCloudLib.deploy import DeploymentError  # noqa: E402


class SyncJob:
    """Runs the task inline; `defer` keeps it un-finished so a test can look."""

    pending = []
    defer = False

    def __init__(self, target, on_success=None, on_error=None, on_progress=None):
        self._target = target
        self._on_success = on_success
        self._on_error = on_error
        self._on_progress = on_progress

    def start(self):
        if SyncJob.defer:
            SyncJob.pending.append(self)
            return
        self.finish()

    def finish(self):
        try:
            result = self._target(lambda message: None)
        except Exception as exc:  # noqa: BLE001 - mirrors BackgroundJob._run
            if self._on_error:
                self._on_error(exc)
            return
        if self._on_success:
            self._on_success(result)

    def cancel(self):
        if self in SyncJob.pending:
            SyncJob.pending.remove(self)


class FakeDeployment:
    """Answers like a machine with docker present and nothing installed."""

    def __init__(self, install_dir=None, repo_url=None, branch=None, **_kwargs):
        self.install_dir = install_dir
        self.repo_url = repo_url
        self.branch = branch
        self.status_calls = 0
        self.calls = []          # ordered, so a test can pin what runs before what
        self.is_cloned = False

    def status(self, check_remote=False, progress_cb=None):
        self.status_calls += 1
        return {
            "cloned": False, "repo_root": self.install_dir,
            "git": {"available": True, "version": "git version 2.34.1"},
            "docker": {"available": True, "version": "Docker 29", "daemon": True, "error": None},
            "compose": {"available": True, "version": "v2"},
            "gpu": {"nvidia_runtime": False, "nvidia_smi": False},
            "clone": {"is_git_repo": False, "behind": 0, "error": "not installed yet"},
            "container": {"running": False, "state": None},
            "server": {"url": "https://localhost:8000", "healthy": False},
            "env": {"has_token": False},
        }

    def catalog(self, progress_cb=None):
        return {"data_dir": "", "disk_free": 10 ** 12, "total_size": 0,
                "total_missing_size": 0, "tools": []}

    def list_tools(self, _url=None):
        return None

    def down(self, progress_cb=None):
        self.calls.append("down")
        return {"service": "inference-cpu", "stopped": True}

    def cancel(self):
        pass

    def can_install_docker(self):
        return True

    @property
    def install_docker_script(self):
        return "/somewhere/scripts/install-docker.sh"

    def clone(self, progress_cb=None):
        self.calls.append("clone")
        return {"cloned": True}

    def enable_gpu_runtime(self, progress_cb=None):
        self.calls.append("enable_gpu_runtime")


class PanelTestCase(unittest.TestCase):
    def setUp(self):
        _reset_dialogs()
        _QSettings.store.clear()
        SyncJob.pending = []
        SyncJob.defer = False
        self._realJob = SlicerCloud.BackgroundJob
        self._realDeployment = SlicerCloud.LocalServerDeployment
        SlicerCloud.BackgroundJob = SyncJob
        SlicerCloud.LocalServerDeployment = FakeDeployment

    def tearDown(self):
        SlicerCloud.BackgroundJob = self._realJob
        SlicerCloud.LocalServerDeployment = self._realDeployment

    def panel(self):
        widget = SlicerCloud.SlicerCloudWidget()
        widget.setup()
        return widget


class TestOpeningThePanel(PanelTestCase):
    def test_the_panel_builds_completely(self):
        """Asserted explicitly, because setup()'s try/except would otherwise
        HIDE a build failure from every other test here: they would all pass
        against a panel that is nothing but an error label.

        This is the check that catches a PythonQt property mismatch — the real
        one was `self.logView.document()`, which raised on a fresh Slicer and
        left _progressLabel and cancelButton uncreated, so every later click
        died on an AttributeError instead of on the actual cause.
        """
        widget = self.panel()
        self.assertFalse(widget._buildFailed, "setup() fell back to its error panel")
        for name in ("installButton", "updateButton", "stopButton", "downloadButton",
                     "refreshButton", "cancelButton", "_progressLabel", "logView",
                     "hintLabel", "installDirEdit", "branchEdit", "portSpin"):
            self.assertTrue(hasattr(widget, name), f"setup() never created {name}")

    def test_a_widget_that_raises_still_leaves_something_usable(self):
        """The safety net itself. A half-built panel is the worst outcome:
        no message, and every click failing somewhere unrelated."""
        original = SlicerCloud.SlicerCloudWidget._buildLogBox

        def boom(_self):
            raise RuntimeError("simulated PythonQt mismatch")

        SlicerCloud.SlicerCloudWidget._buildLogBox = boom
        try:
            widget = SlicerCloud.SlicerCloudWidget()
            widget.setup()
        finally:
            SlicerCloud.SlicerCloudWidget._buildLogBox = original

        self.assertTrue(widget._buildFailed)
        # The two widgets every error path needs exist even so...
        self.assertTrue(hasattr(widget, "_progressLabel"))
        self.assertTrue(hasattr(widget, "cancelButton"))
        # ...and nothing runs against the wreckage.
        widget.onRefresh()
        widget.enter()
        self.assertEqual(DIALOGS["error"], [])

    def test_install_is_clickable_as_soon_as_the_panel_opens(self):
        """The reported bug, exactly: a fresh panel whose Install button cannot
        be pressed. The status check is passive and must never disable it."""
        widget = self.panel()
        self.assertTrue(widget.installButton.isEnabled())
        self.assertTrue(widget.refreshButton.isEnabled())

    def test_opening_the_panel_pops_no_dialog(self):
        """`_loadSettings` assigning currentPath used to start a refresh, so
        setup()'s own refresh hit "Something is already running" — a modal on
        every single module open."""
        self.panel()
        self.assertEqual(DIALOGS["warning"], [])
        self.assertEqual(DIALOGS["error"], [])

    def test_the_status_is_checked_exactly_once(self):
        widget = self.panel()
        self.assertEqual(widget.deployment().status_calls, 1)

    def test_the_panel_says_what_to_do_next(self):
        widget = self.panel()
        self.assertIn("Install and start", widget.hintLabel.text)

    def test_install_stays_clickable_while_the_check_is_still_running(self):
        """Not merely "enabled once it finished": the check runs on every
        enter(), and on a slow docker probe it is seconds long."""
        SyncJob.defer = True
        widget = self.panel()
        self.assertTrue(SyncJob.pending, "the status check did not start")
        self.assertTrue(widget.installButton.isEnabled())
        SyncJob.pending[0].finish()
        self.assertTrue(widget.installButton.isEnabled())


class TestJobArbitration(PanelTestCase):
    def test_real_work_supersedes_a_running_status_check(self):
        """A background check must never make the user wait for a button."""
        SyncJob.defer = True
        widget = self.panel()
        self.assertTrue(SyncJob.pending)

        SyncJob.defer = False
        widget.onStop()
        self.assertEqual(DIALOGS["warning"], [], "the check refused to step aside")

    def test_a_second_real_job_is_refused_rather_than_interleaved(self):
        SyncJob.defer = True
        widget = self.panel()
        SyncJob.pending[0].finish()
        SyncJob.pending = []

        widget.onStop()          # deferred: stays running
        widget.onStop()          # must be refused
        self.assertEqual(len(DIALOGS["warning"]), 1)

    def test_a_real_job_disables_the_buttons_and_shows_cancel(self):
        SyncJob.defer = True
        widget = self.panel()
        SyncJob.pending[0].finish()
        SyncJob.pending = []

        widget.onStop()
        self.assertFalse(widget.installButton.isEnabled())
        self.assertTrue(widget.cancelButton.isVisible())

        SyncJob.pending[0].finish()
        self.assertTrue(widget.installButton.isEnabled())
        self.assertFalse(widget.cancelButton.isVisible())


class TestChangingTheInstallFolder(PanelTestCase):
    def test_picking_a_folder_re_checks_that_folder(self):
        widget = self.panel()
        first = widget.deployment()
        widget.installDirEdit.currentPath = "/tmp/elsewhere"
        self.assertIsNot(widget.deployment(), first)
        self.assertEqual(widget.deployment().install_dir, "/tmp/elsewhere")
        self.assertEqual(DIALOGS["warning"], [])

    def test_the_folder_is_remembered(self):
        widget = self.panel()
        widget.installDirEdit.currentPath = "/tmp/remembered"
        self.assertEqual(_QSettings.store["SlicerCloud/InstallDir"], "/tmp/remembered")


class TestServerUrlIsHttpsOnly(PanelTestCase):
    """The URL an install hands back becomes every module's server, so the
    rules apply here too: never http://, loopback included, and only the
    deployment's public URL -- never the local self-signed listener, which the
    client refuses to verify."""

    def setUp(self):
        PanelTestCase.setUp(self)
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)

    def _install_answering(self, answer):
        class Recording(FakeDeployment):
            def up(self, progress_cb=None, force_recreate=False, port=None, bind=None):
                return dict(answer)

        SlicerCloud.LocalServerDeployment = Recording
        widget = self.panel()
        widget._deployment = None
        widget.useServerCheck.checked = True
        widget.onInstall()
        return widget

    def test_every_http_url_is_refused_and_nothing_is_saved(self):
        widget = self.panel()
        before = SlicerCloud.get_client().server_url
        for url in ("http://localhost:8000", "http://192.0.2.1:8000"):
            widget._applyServerSettings(url, "t")
        self.assertEqual(SlicerCloud.get_client().server_url, before)
        self.assertNotIn("ServerTools/ServerUrl", _QSettings.store)
        self.assertEqual(len(DIALOGS["error"]), 2)
        self.assertIn("https://", DIALOGS["error"][0])

    def test_the_public_url_is_what_the_client_is_given(self):
        self._install_answering({"url": "https://localhost:8000", "token": "t", "healthy": True,
                                 "public_url": "https://visor.example.ts.net"})
        self.assertEqual(SlicerCloud.get_client().server_url, "https://visor.example.ts.net")
        self.assertEqual(_QSettings.store["ServerTools/ServerUrl"], "https://visor.example.ts.net")

    def test_without_a_public_url_nothing_is_saved_and_the_panel_says_so(self):
        before = SlicerCloud.get_client().server_url
        self._install_answering({"url": "https://localhost:8000", "token": "t", "healthy": True,
                                 "public_url": ""})
        self.assertEqual(SlicerCloud.get_client().server_url, before)
        self.assertNotIn("ServerTools/ServerUrl", _QSettings.store)
        self.assertTrue(any("no public address yet" in text and "PUBLIC_URL" in text
                            for text in DIALOGS["warning"]))


class TestDockerMissing(PanelTestCase):
    def test_install_explains_instead_of_starting_a_doomed_job(self):
        """With docker absent the panel must say what to run, not clone and
        then fail somewhere deeper."""
        class NoDocker(FakeDeployment):
            def status(self, check_remote=False, progress_cb=None):
                status = FakeDeployment.status(self, check_remote, progress_cb)
                status["docker"] = {"available": False, "version": None,
                                    "daemon": False, "error": "docker is not in PATH"}
                return status

            def can_install_docker(self):
                return False

            @property
            def install_docker_script(self):
                return "/somewhere/scripts/install-docker.sh"

        SlicerCloud.LocalServerDeployment = NoDocker
        widget = self.panel()
        widget.onInstall()
        self.assertEqual(len(DIALOGS["error"]), 1)
        self.assertIn("install-docker.sh", DIALOGS["error"][0])


class TestNetworkAccess(PanelTestCase):
    """Who can reach the server. The default has to stay loopback, the choice
    has to survive an update, and leaving loopback has to be said out loud --
    this deployment carries medical images under a certificate only this
    computer can verify."""

    def setUp(self):
        PanelTestCase.setUp(self)
        # A fixed set of addresses, so the tests do not depend on whatever
        # interfaces the machine running them happens to have.
        self._realAddresses = SlicerCloud.deploy.host_addresses
        SlicerCloud.deploy.host_addresses = lambda: [
            ("127.0.0.1", "This computer only", "loopback"),
            ("100.94.102.62", "tailscale0 — private network, encrypted", "vpn"),
            ("192.168.1.20", "eth0 — local network", "lan"),
            ("", "Every network on this machine", "all"),
        ]

    def tearDown(self):
        SlicerCloud.deploy.host_addresses = self._realAddresses
        PanelTestCase.tearDown(self)

    def _select(self, widget, address):
        for index in range(widget.bindCombo.count):
            if str(widget.bindCombo.itemData(index)) == address:
                widget.bindCombo.setCurrentIndex(index)
                return
        raise AssertionError(f"{address!r} is not offered")

    def test_it_defaults_to_this_computer_only(self):
        """A server holding patient data must never become network-reachable
        because someone pressed Install without reading a combo box."""
        widget = self.panel()
        self.assertEqual(widget.selectedBind(), "127.0.0.1")

    def test_docker_bridges_are_not_offered(self):
        """Binding to docker0 publishes the server to containers and nothing
        else -- an entry that can only ever be a mistake."""
        SlicerCloud.deploy.host_addresses = self._realAddresses
        offered = [address for address, _label, _kind in SlicerCloud.deploy.host_addresses()]
        for address in offered:
            self.assertFalse(address.startswith(("172.17.", "172.18.", "172.19.")),
                             f"{address} is a docker bridge")

    def test_choosing_a_lan_address_warns_that_no_other_machine_can_verify_it(self):
        widget = self.panel()
        self._select(widget, "192.168.1.20")
        self.assertEqual(len(DIALOGS["warning"]), 1)
        self.assertIn("cannot verify it", DIALOGS["warning"][0])

    def test_a_vpn_address_is_not_presented_as_the_same_risk(self):
        """Tailscale/WireGuard encrypt the link, so this one is informative
        rather than a warning -- and lumping the two together would train
        people to dismiss both."""
        widget = self.panel()
        self._select(widget, "100.94.102.62")
        self.assertEqual(DIALOGS["warning"], [])
        self.assertEqual(len(DIALOGS["info"]), 1)
        self.assertIn("100.94.102.62", DIALOGS["info"][0])

    def test_publishing_everywhere_is_a_warning_too(self):
        widget = self.panel()
        self._select(widget, "")
        self.assertEqual(len(DIALOGS["warning"]), 1)

    def test_the_choice_reaches_the_container(self):
        widget = self.panel()
        self._select(widget, "100.94.102.62")

        captured = {}

        class Recording(FakeDeployment):
            def up(self, progress_cb=None, force_recreate=False, port=None, bind=None):
                captured["bind"] = bind
                return {"url": "https://100.94.102.62:8000", "token": "t", "healthy": True}

        SlicerCloud.LocalServerDeployment = Recording
        widget._deployment = None          # rebuild against the recording class
        widget.onInstall()
        self.assertEqual(captured.get("bind"), "100.94.102.62")

    def test_an_update_does_not_quietly_take_the_server_off_the_network(self):
        """`update` recreates the container, so anything not restated goes back
        to its default -- and the default is loopback. This is the same trap the
        Branch field fell into, one field over."""
        widget = self.panel()
        self._select(widget, "100.94.102.62")

        captured = {}

        class Recording(FakeDeployment):
            def update(self, progress_cb=None, force=False, bind=None):
                captured["bind"] = bind
                return {"clone": {}, "pulled": False, "recreated": True, "healthy": True}

        SlicerCloud.LocalServerDeployment = Recording
        widget._deployment = None
        widget.onUpdate()
        self.assertEqual(captured.get("bind"), "100.94.102.62")

    def test_an_address_that_disappeared_falls_back_to_loopback(self):
        """A VPN goes down, DHCP hands out a different address. Keeping the
        stale one means docker refuses to bind and the server stops starting;
        falling back to loopback is the safe direction."""
        _QSettings.store[SlicerCloud._KEY_BIND] = "10.99.99.99"
        widget = self.panel()
        self.assertEqual(widget.selectedBind(), "127.0.0.1")

    def test_publishing_everywhere_survives_a_reload(self):
        """BIND_EVERYWHERE is the empty string, so a falsy test would read a
        saved 'every interface' as 'never chosen' and silently re-lock it."""
        widget = self.panel()
        self._select(widget, "")
        self.assertEqual(_QSettings.store.get(SlicerCloud._KEY_BIND), "")
        reopened = self.panel()
        self.assertEqual(reopened.selectedBind(), "")


class TestDockerGroup(PanelTestCase):
    """Docker is installed and running; the account is just not in the group.
    The most common way a first install stops, and the one the panel can only
    answer with instructions — the fix needs root AND a new session."""

    def _panelOutsideTheGroup(self):
        class OutsideGroup(FakeDeployment):
            def status(self, check_remote=False, progress_cb=None):
                status = FakeDeployment.status(self, check_remote, progress_cb)
                status["docker"] = {
                    "available": True, "version": "Docker 29", "daemon": False,
                    "error": "permission denied while trying to connect", "needs_group": True,
                }
                return status

        SlicerCloud.LocalServerDeployment = OutsideGroup
        return self.panel()

    def test_it_does_not_offer_to_reinstall_docker(self):
        """What it used to do: ask for a password, run the installer for
        minutes, add the user to a group they are already in, change nothing."""
        widget = self._panelOutsideTheGroup()
        widget.onInstall()
        self.assertEqual(DIALOGS["confirm"], [], "the panel offered to install Docker again")
        self.assertEqual(len(DIALOGS["info"]), 1)

    def test_the_instructions_are_numbered_steps_not_a_paragraph(self):
        widget = self._panelOutsideTheGroup()
        widget.onInstall()
        shown = DIALOGS["info"][0]
        for step in ("1.", "2.", "3.", "4.", "5.", "6."):
            self.assertIn(step, shown)

    def test_every_step_someone_was_watched_to_fail_is_spelled_out(self):
        """Each of these was a real dead end, not a hypothetical: no terminal
        open, Ctrl+V losing the command, and a password prompt that echoes
        nothing reading as a frozen terminal."""
        widget = self._panelOutsideTheGroup()
        widget.onInstall()
        shown = DIALOGS["info"][0]
        self.assertIn("Ctrl+Alt+T", shown)
        self.assertIn("Ctrl+Shift+V", shown)
        self.assertIn("Nothing appears", shown)
        self.assertIn("usermod -aG docker", shown)
        self.assertIn("Log out", shown)

    def test_the_command_names_the_real_account(self):
        """`$USER` in a dialog is a shell variable someone has to know expands —
        and it does not, if they paste into a root shell."""
        widget = self._panelOutsideTheGroup()
        widget.onInstall()
        self.assertIn(getpass.getuser(), DIALOGS["info"][0])
        self.assertNotIn("$USER", DIALOGS["info"][0])

    def test_the_status_grid_stays_a_one_liner(self):
        """Six numbered steps belong in the dialog. A status cell that grows to
        twenty lines pushes every row below it off the panel."""
        widget = self._panelOutsideTheGroup()
        self.assertNotIn("\n", widget._statusRows["docker"].text)

    def test_the_hint_points_at_the_button_that_explains(self):
        widget = self._panelOutsideTheGroup()
        self.assertIn("Install and start", widget.hintLabel.text)


def _withGpu(**gpu):
    """A deployment whose status reports `gpu` as given."""
    class WithGpu(FakeDeployment):
        def status(self, check_remote=False, progress_cb=None):
            status = FakeDeployment.status(self, check_remote, progress_cb)
            status["gpu"] = gpu
            return status
    return WithGpu


class TestGpuSetup(PanelTestCase):
    """The card is present, docker cannot reach it, and nobody should have to
    open a terminal to fix that."""

    def test_no_card_means_no_button(self):
        """The default fixture has no GPU at all. Offering to set one up there
        is one more thing to understand before pressing Install."""
        widget = self.panel()
        self.assertFalse(widget.gpuButton.isVisible())

    def test_a_card_docker_cannot_reach_offers_the_button(self):
        SlicerCloud.LocalServerDeployment = _withGpu(nvidia_runtime=False, nvidia_smi=True)
        widget = self.panel()
        self.assertTrue(widget.gpuButton.isVisible())

    def test_a_gpu_reached_through_cdi_is_not_a_problem_to_fix(self):
        """The bug this whole change came from: docker reaches the card through
        CDI with no 'nvidia' runtime registered anywhere. That host must read as
        working — not be offered a repair for something that is not broken."""
        SlicerCloud.LocalServerDeployment = _withGpu(
            nvidia_runtime=True, nvidia_smi=True, gpu_access="cdi")
        widget = self.panel()
        self.assertFalse(widget.gpuButton.isVisible())
        self.assertIn("CDI", widget._statusRows["gpu"].text)

    def test_a_clone_that_predates_gpu_access_still_reads_correctly(self):
        """`gpu_access` comes from the CLONE's server_ctl.py, which can be older
        than this panel. Its absence must not turn a working GPU into unknown."""
        SlicerCloud.LocalServerDeployment = _withGpu(nvidia_runtime=True, nvidia_smi=True)
        widget = self.panel()
        self.assertFalse(widget.gpuButton.isVisible())
        self.assertIn("nvidia runtime", widget._statusRows["gpu"].text)

    def test_the_server_is_stopped_before_docker_is_restarted(self):
        """Ordering, and it is the whole correctness of the feature. The compose
        services carry `restart: unless-stopped`, so a container still running
        when the daemon restarts comes back and holds port 8000 against the GPU
        service that replaces it — a different compose service, same port."""
        SlicerCloud.LocalServerDeployment = _withGpu(nvidia_runtime=False, nvidia_smi=True)
        widget = self.panel()
        widget._offerGpuSetup()
        calls = widget.deployment().calls
        self.assertIn("enable_gpu_runtime", calls)
        self.assertLess(calls.index("down"), calls.index("enable_gpu_runtime"))

    def test_the_password_prompt_is_announced_with_its_consequence(self):
        SlicerCloud.LocalServerDeployment = _withGpu(nvidia_runtime=False, nvidia_smi=True)
        widget = self.panel()
        widget._offerGpuSetup()
        self.assertEqual(len(DIALOGS["confirm"]), 1)
        asked = DIALOGS["confirm"][0]
        self.assertIn("password", asked)
        self.assertIn("RESTARTS DOCKER", asked)

    def test_a_host_with_no_graphical_helper_is_told_what_to_run(self):
        """No pkexec, no DISPLAY: the button must not start a job that cannot
        possibly get root — it names the terminal command instead."""
        base = _withGpu(nvidia_runtime=False, nvidia_smi=True)

        class NoPkexec(base):
            def can_install_docker(self):
                return False

        SlicerCloud.LocalServerDeployment = NoPkexec
        widget = self.panel()
        widget._offerGpuSetup()
        self.assertEqual(len(DIALOGS["error"]), 1)
        self.assertIn("--nvidia", DIALOGS["error"][0])
        self.assertNotIn("enable_gpu_runtime", widget.deployment().calls)

    def test_the_gpu_hint_never_displaces_a_blocking_step(self):
        """The server WORKS on the CPU. A card docker cannot reach is an
        improvement, so it must not push aside 'press Install and start'."""
        SlicerCloud.LocalServerDeployment = _withGpu(nvidia_runtime=False, nvidia_smi=True)
        widget = self.panel()
        self.assertIn("Install and start", widget.hintLabel.text)


if __name__ == "__main__":
    unittest.main()
