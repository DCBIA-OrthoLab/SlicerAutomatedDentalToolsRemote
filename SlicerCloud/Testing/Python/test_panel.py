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
from unittest import mock

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
ANSWER = {"yes": True}


def _reset_dialogs():
    ANSWER["yes"] = True
    for value in DIALOGS.values():
        del value[:]


slicer.util = types.SimpleNamespace(
    errorDisplay=lambda text, **_k: DIALOGS["error"].append(text),
    warningDisplay=lambda text, **_k: DIALOGS["warning"].append(text),
    infoDisplay=lambda text, **_k: DIALOGS["info"].append(text),
    showStatusMessage=lambda *_a, **_k: None,
    confirmOkCancelDisplay=lambda text, *_a, **_k: (DIALOGS["confirm"].append(text) or True),
    confirmYesNoDisplay=lambda text, *_a, **_k: (DIALOGS["confirm"].append(text) or ANSWER["yes"]),
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
from SlicerCloudLib import backends  # noqa: E402


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

    backend = backends.DOCKER

    def __init__(self, install_dir=None, repo_url=None, branch=None, **kwargs):
        self.tools_repo_url = kwargs.get("tools_repo_url")
        self.tools_branch = kwargs.get("tools_branch")
        self.install_dir = install_dir
        self.repo_url = repo_url
        self.branch = branch
        self.status_calls = 0
        self.calls = []          # ordered, so a test can pin what runs before what
        self.is_cloned = False
        self.switches = []       # what pending_branch_switches() reports
        self.after = {}          # path -> branch once the job has run

    def pending_branch_switches(self):
        return list(self.switches)

    def current_branch(self, path):
        return self.after.get(path, "")

    def require_native_support(self, progress_cb=None):
        self.calls.append("require_native_support")

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
        # The panel tests describe the Docker path; on an Apple-silicon runner
        # the native backend would otherwise be chosen (and the bind fixed).
        patcher = mock.patch.object(SlicerCloud.deploy, "is_apple_silicon", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # What only this machine can say: not the machine running the tests.
        for name, value in (("port_in_use", False), ("free_space", 100 * 1024 ** 3)):
            hostPatch = mock.patch.object(SlicerCloud.deploy, name, return_value=value)
            hostPatch.start()
            self.addCleanup(hostPatch.stop)
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


class TestToolsRepository(PanelTestCase):
    """The native server's tools repository and branch: Advanced fields that
    are remembered like the others and travel to the deployment, in developer
    mode (outside it they are ignored: see TestDeveloperMode)."""

    def setUp(self):
        PanelTestCase.setUp(self)
        _QSettings.store["Developer/DeveloperMode"] = True

    def test_the_defaults_are_the_compiled_in_ones(self):
        from SlicerCloudLib import deploy
        widget = self.panel()
        self.assertEqual(widget.toolsRepoUrlEdit.text, deploy.DEFAULT_TOOLS_REPO_URL)
        self.assertEqual(widget.deployment().tools_repo_url, deploy.DEFAULT_TOOLS_REPO_URL)
        self.assertEqual(widget.deployment().tools_branch, deploy.DEFAULT_TOOLS_BRANCH)

    def test_a_new_url_rebuilds_the_deployment_and_is_remembered(self):
        widget = self.panel()
        first = widget.deployment()
        widget.toolsRepoUrlEdit.text = "  https://example.org/fork/SADT-VISOR.git "
        self.assertIsNot(widget.deployment(), first)
        self.assertEqual(widget.deployment().tools_repo_url, "https://example.org/fork/SADT-VISOR.git")
        widget._saveSettings()
        self.assertEqual(_QSettings.store["SlicerCloud/ToolsRepoUrl"],
                         "https://example.org/fork/SADT-VISOR.git")
        reopened = self.panel()
        self.assertEqual(reopened.toolsRepoUrlEdit.text, "https://example.org/fork/SADT-VISOR.git")

    def test_an_emptied_field_falls_back_to_the_default(self):
        from SlicerCloudLib import deploy
        widget = self.panel()
        widget.toolsRepoUrlEdit.text = "   "
        self.assertEqual(widget.deployment().tools_repo_url, deploy.DEFAULT_TOOLS_REPO_URL)


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


class NativeFake(FakeDeployment):
    """The native server on an Apple-silicon Mac: no Docker, a pinned URL."""

    backend = backends.NATIVE
    native = True
    reported = {"url": "https://127.0.0.1:8000", "token": "t", "healthy": True,
                "tls": {"enabled": True}}

    def status(self, check_remote=False, progress_cb=None):
        status = FakeDeployment.status(self)
        status["docker"] = {"available": False, "version": None, "daemon": False,
                            "error": "not used by the native backend"}
        status["compose"] = {"available": False, "version": None}
        status["gpu"] = {"nvidia_runtime": False, "nvidia_smi": False, "gpu_access": "mps"}
        return status

    def clone_tools(self, progress_cb=None):
        self.calls.append("clone_tools")

    def install_tools(self, progress_cb=None, **_kwargs):
        self.calls.append("install_tools")

    def up(self, progress_cb=None, force_recreate=False, port=None, bind=None):
        self.calls.append(("up", port, bind))
        return dict(self.reported)


class TestNativeServerPanel(PanelTestCase):
    """A Mac with Apple silicon: no Docker step, https://127.0.0.1 pinned."""

    def setUp(self):
        PanelTestCase.setUp(self)
        import tempfile
        import shutil
        from ServerToolsCoreLib import tls
        from SlicerCloudLib import local_cert
        self.work = tempfile.mkdtemp(prefix="panel_native_")
        self.addCleanup(shutil.rmtree, self.work, True)
        pin_dir = os.path.join(self.work, "tls")
        patcher = mock.patch.object(tls, "local_tls_dir", return_value=pin_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.local_cert = local_cert
        self.pin_dir = pin_dir
        mock.patch.object(SlicerCloud.deploy, "is_apple_silicon", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)
        SlicerCloud.LocalServerDeployment = NativeFake

    def installed(self):
        widget = self.panel()
        widget.useServerCheck.checked = True
        widget.onInstall()
        return widget

    def test_install_is_clone_tools_build_then_start_without_any_docker_step(self):
        self.local_cert.ensure_local_certificate(self.pin_dir)
        widget = self.installed()
        self.assertEqual(widget.deployment().calls,
                         ["require_native_support", "clone", "clone_tools", "install_tools",
                          ("up", None, "127.0.0.1")])
        self.assertEqual(DIALOGS["error"], [])

    def test_the_client_is_pointed_at_the_pinned_local_address(self):
        self.local_cert.ensure_local_certificate(self.pin_dir)
        self.installed()
        self.assertEqual(SlicerCloud.get_client().server_url, "https://127.0.0.1:8000")
        self.assertEqual(_QSettings.store["ServerTools/ServerUrl"], "https://127.0.0.1:8000")

    def test_no_pinned_certificate_means_nothing_is_saved_and_the_panel_says_why(self):
        before = SlicerCloud.get_client().server_url
        self.installed()
        self.assertEqual(SlicerCloud.get_client().server_url, before)
        self.assertNotIn("ServerTools/ServerUrl", _QSettings.store)
        self.assertTrue(any("serves HTTPS" in text for text in DIALOGS["warning"]))
        self.assertFalse(any("no public address" in text for text in DIALOGS["warning"]))

    def test_the_bind_is_fixed_to_this_computer(self):
        widget = self.panel()
        self.assertFalse(widget.bindCombo.isEnabled())
        for index in range(widget.bindCombo.count):
            widget.bindCombo.setCurrentIndex(index)
            self.assertEqual(widget.selectedBind(), "127.0.0.1")

    def test_the_status_rows_describe_this_backend(self):
        widget = self.panel()
        self.assertIn("Apple GPU", widget._statusRows["gpu"].text)
        self.assertNotIn("Docker is missing", widget.hintLabel.text)
        self.assertIn("Set up VISOR Local", widget.hintLabel.text)
        self.assertEqual(widget.installButton.text, "Set up VISOR Local")

    def test_no_row_describes_something_a_mac_does_not_use(self):
        """Docker, Compose and the container were three rows of 'not needed'."""
        widget = self.panel()
        for key in ("docker", "compose", "container"):
            self.assertFalse(widget._statusRows[key].isVisible(), key)
            self.assertFalse(widget._statusNames[key].isVisible(), key)
        for key in ("git", "uv", "gpu", "disk", "port", "clone", "health"):
            self.assertTrue(widget._statusRows[key].isVisible(), key)


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
        # The network choice only counts in developer mode (TestDeveloperMode).
        _QSettings.store["Developer/DeveloperMode"] = True
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

    def test_choosing_a_lan_address_warns_that_a_publicly_signed_address_is_needed(self):
        widget = self.panel()
        self._select(widget, "192.168.1.20")
        self.assertEqual(len(DIALOGS["warning"]), 1)
        self.assertIn("publicly-signed", DIALOGS["warning"][0])

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
        self.assertEqual(widget.installButton.text, "Fix: Show the steps")
        self.assertIn("Fix: Show the steps", widget.hintLabel.text)


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


class MismatchFake(FakeDeployment):
    """A clone that is cloned, healthy and running, but on another branch
    than the form asks for: the case of every developer testing a branch."""

    def status(self, check_remote=False, progress_cb=None):
        status = FakeDeployment.status(self)
        status.update({
            "cloned": True, "service": "inference-cpu",
            "clone": {"is_git_repo": True, "branch": "feat/x", "configured_branch": "main",
                      "branch_mismatch": True, "behind": 0, "commit": "abc1234"},
            "container": {"running": True, "state": "running"},
            "server": {"url": "https://localhost:8000", "healthy": True},
        })
        return status


class TestBranchMismatchStillPaintsEverything(PanelTestCase):
    """`_renderStatus` used to return right after the clone row when the
    branch differed, leaving the badge, the Stop button and the hint as they
    were: precisely the state of anyone testing a non-default branch."""

    def test_the_badge_the_rows_the_hint_and_stop_are_all_current(self):
        SlicerCloud.LocalServerDeployment = MismatchFake
        widget = self.panel()
        self.assertEqual(widget._statusBadge.text, "Server: online")
        self.assertIn("running", widget._statusRows["container"].text)
        self.assertIn("answering", widget._statusRows["health"].text)
        self.assertNotEqual(widget.hintLabel.text, "")
        self.assertTrue(widget.stopButton.isEnabled())
        self.assertIn("feat/x", widget._statusRows["clone"].text)

    def test_the_hint_names_the_switch(self):
        SlicerCloud.LocalServerDeployment = MismatchFake
        widget = self.panel()
        self.assertIn("feat/x", widget.hintLabel.text)


class TestUpdateSummary(PanelTestCase):
    """What an update says it did, for the result each backend returns."""

    def update(self, result):
        class Upd(FakeDeployment):
            def update(self, progress_cb=None, force=False, bind=None):
                return dict(result)
        SlicerCloud.LocalServerDeployment = Upd
        widget = self.panel()
        widget.onUpdate()
        return "\n".join(widget.logView.lines)

    def test_a_native_update_that_only_pulled_the_tools_is_not_nothing(self):
        logged = self.update({"pulled": False, "tools_pulled": True, "venvs": [],
                              "restarted": True, "healthy": True, "clone": {}})
        self.assertNotIn("nothing to do", logged)
        self.assertIn("tools", logged.lower())
        self.assertIn("relaunched", logged)

    def test_rebuilt_environments_are_counted(self):
        logged = self.update({"pulled": False, "tools_pulled": False, "healthy": True,
                              "venvs": [{"tool": "ALI", "action": "built"},
                                        {"tool": "AREG", "action": "built"},
                                        {"tool": "CNE", "action": "unchanged"}],
                              "restarted": True, "clone": {}})
        self.assertNotIn("nothing to do", logged)
        self.assertIn("2 tool environment(s)", logged)

    def test_a_native_update_with_nothing_new_says_so(self):
        logged = self.update({"pulled": False, "tools_pulled": False, "venvs": [],
                              "restarted": False, "healthy": True, "clone": {}})
        self.assertIn("nothing to do", logged)

    def test_the_docker_summaries_are_unchanged(self):
        self.assertIn("Updated to abc and relaunched",
                      self.update({"pulled": True, "recreated": True, "clone": {"commit": "abc"}}))
        self.assertIn("the container was relaunched",
                      self.update({"pulled": False, "recreated": True, "clone": {}}))
        self.assertIn("nothing to do", self.update({"pulled": False, "recreated": False, "clone": {}}))


class UvMissingFake(NativeFake):
    """A Mac with Apple silicon that has no uv: the probe before the clone says so."""

    def status(self, check_remote=False, progress_cb=None):
        status = NativeFake.status(self)
        status["uv"] = None
        return status


class TestUvIsCheckedBeforeAnythingIsCloned(PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        mock.patch.object(SlicerCloud.deploy, "is_apple_silicon", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        SlicerCloud.LocalServerDeployment = UvMissingFake

    def test_install_stops_with_the_remedy_and_runs_no_job(self):
        widget = self.panel()
        widget.onInstall()
        self.assertEqual(widget.deployment().calls, [])
        self.assertEqual(DIALOGS["error"], [])
        self.assertEqual(len(DIALOGS["info"]), 1)
        self.assertIn("uv", DIALOGS["info"][0])
        self.assertIn("astral.sh/uv", DIALOGS["info"][0])
        self.assertEqual(widget.installButton.text, "Fix: Install uv")

    def test_the_hint_says_it_before_the_press(self):
        widget = self.panel()
        self.assertIn("uv", widget.hintLabel.text)


import copy  # noqa: E402

import fixtures  # noqa: E402

LOCAL_URL = "https://127.0.0.1:8000"
REMOTE_URL = "https://lab.example.org"
PUBLIC_URL = "https://box.example.ts.net"


def serving(status, backend=backends.DOCKER, **members):
    """A deployment class whose status() is `status` (a fixtures.* dict)."""
    namespace = {
        "backend": backend,
        "status": lambda self, check_remote=False, progress_cb=None: copy.deepcopy(status),
        "token": lambda self, progress_cb=None: "the-token",
        "up": lambda self, progress_cb=None, force_recreate=False, port=None, bind=None: (
            self.calls.append(("up", port, bind)) or {"url": LOCAL_URL, "token": "t", "healthy": True}),
    }
    namespace.update(members)
    return type("Serving", (FakeDeployment,), namespace)


def visible(widget):
    """Which of the panel's buttons are on screen, by name."""
    names = ("installButton", "updateButton", "stopButton", "useButton", "portButton",
             "refreshButton", "checkUpdateButton", "showLogsButton", "switchBackButton", "gpuButton",
             "cancelButton")
    return {name for name in names if getattr(widget, name).isVisible()}


class TestTheStateDecidesTheButtons(PanelTestCase):
    """The panel used to show every button in every state: 'Start server' beside a
    running server. Hidden, not greyed, from the state alone."""

    def show(self, status, backend=backends.DOCKER, client=None):
        SlicerCloud.LocalServerDeployment = serving(status, backend)
        if client:
            SlicerCloud.get_client().configure(server_url=client, token="x")
        return self.panel()

    def setUp(self):
        PanelTestCase.setUp(self)
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)
        SlicerCloud.get_client().configure(server_url="https://set-your-server.invalid", token="")

    def test_a_running_docker_server_has_no_start_button(self):
        widget = self.show(fixtures.docker(server={"public_url": PUBLIC_URL}), client=PUBLIC_URL)
        self.assertNotIn("installButton", visible(widget))
        self.assertIn("stopButton", visible(widget))

    def test_a_running_native_server_has_no_start_button(self):
        widget = self.show(fixtures.native(), backends.NATIVE, client=LOCAL_URL)
        self.assertEqual(visible(widget),
                         {"stopButton", "refreshButton", "checkUpdateButton", "showLogsButton"})
        self.assertEqual(widget._statusBadge.text, "Server: online")

    def test_nothing_installed_offers_only_the_install(self):
        widget = self.show(fixtures.docker_uninstalled())
        self.assertEqual(visible(widget), {"installButton", "refreshButton"})
        self.assertEqual(widget.installButton.text, "Install and start")

    def test_a_stopped_server_offers_start_and_hides_stop(self):
        widget = self.show(fixtures.native(container={"running": False}, server={"healthy": False}),
                           backends.NATIVE)
        self.assertIn("installButton", visible(widget))
        self.assertNotIn("stopButton", visible(widget))
        self.assertEqual(widget.installButton.text, "Start VISOR Local")

    def test_update_is_offered_only_when_there_is_something_to_update_to(self):
        behind = self.show(fixtures.native(clone={"behind": 2}), backends.NATIVE, client=LOCAL_URL)
        self.assertIn("updateButton", visible(behind))
        current = self.show(fixtures.native(), backends.NATIVE, client=LOCAL_URL)
        self.assertNotIn("updateButton", visible(current))

    def test_a_running_server_the_modules_do_not_use_offers_to_be_used(self):
        widget = self.show(fixtures.native(), backends.NATIVE, client=REMOTE_URL)
        self.assertIn("useButton", visible(widget))
        self.assertEqual(widget.useButton.text, "Use VISOR Local")
        here = self.show(fixtures.native(), backends.NATIVE, client=LOCAL_URL)
        self.assertNotIn("useButton", visible(here))

    def test_a_missing_prerequisite_offers_its_fix_and_nothing_else(self):
        widget = self.show(fixtures.native_uninstalled(uv=None), backends.NATIVE)
        self.assertEqual(visible(widget), {"installButton", "refreshButton"})
        self.assertEqual(widget.installButton.text, "Fix: Install uv")

    def test_only_cancel_is_left_while_installing(self):
        SyncJob.defer = True
        widget = self.show(fixtures.native_uninstalled(), backends.NATIVE)
        SyncJob.pending[0].finish()
        SyncJob.pending = []
        widget.onInstall()
        self.assertEqual(visible(widget), {"cancelButton"})
        SyncJob.pending[0].finish()
        self.assertNotIn("cancelButton", visible(widget))
        self.assertIn("refreshButton", visible(widget))

    def test_the_buttons_come_back_when_a_job_fails(self):
        class Failing(serving(fixtures.native_uninstalled(), backends.NATIVE)):
            def require_native_support(self, progress_cb=None):
                raise SlicerCloud.DeploymentError("nope")

        SlicerCloud.LocalServerDeployment = Failing
        widget = self.panel()
        widget.onInstall()
        self.assertIn("installButton", visible(widget))
        self.assertNotIn("cancelButton", visible(widget))

    def test_the_gpu_link_shows_in_a_state_that_can_use_it(self):
        widget = self.show(fixtures.docker(container={"running": False}, server={"healthy": False},
                                           gpu={"nvidia_smi": True, "nvidia_runtime": False}))
        self.assertIn("gpuButton", visible(widget))

    def test_every_backend_every_state_has_a_non_empty_hint(self):
        for backend, status in ((backends.DOCKER, fixtures.docker()),
                                (backends.NATIVE, fixtures.native()),
                                (backends.DOCKER, fixtures.docker_uninstalled()),
                                (backends.NATIVE, fixtures.native_uninstalled())):
            widget = self.show(status, backend)
            self.assertTrue(widget.hintLabel.text, (backend.name, status.get("cloned")))

    def test_nothing_a_native_user_can_read_mentions_docker(self):
        for status in (fixtures.native(), fixtures.native_uninstalled(),
                       fixtures.native(container={"running": False}, server={"healthy": False}),
                       fixtures.native_uninstalled(uv=None)):
            widget = self.show(status, backends.NATIVE)
            said = [widget.hintLabel.text, widget._serverBox.text, widget.installButton.text,
                    widget.installButton.toolTip(), widget.updateButton.toolTip(),
                    widget.stopOnExitCheck.toolTip(), widget.bindCombo.toolTip()]
            for key, label in widget._statusRows.items():
                if label.isVisible():
                    said.append(label.text)
            for text in said:
                self.assertNotIn("docker", text.lower(), text)
                self.assertNotIn("container", text.lower(), text)


class TestInstallFolder(PanelTestCase):
    def folder(self, status, backend=backends.DOCKER):
        SlicerCloud.LocalServerDeployment = serving(status, backend)
        return self.panel()

    def test_it_is_offered_with_the_free_space_before_the_first_install(self):
        widget = self.folder(fixtures.native_uninstalled(), backends.NATIVE)
        self.assertTrue(widget.installDirEdit.isVisible())
        self.assertIn("100.0 GB free", widget.installDirNote.text)

    def test_once_installed_it_is_stated_not_offered(self):
        widget = self.folder(fixtures.native(), backends.NATIVE)
        self.assertFalse(widget.installDirEdit.isVisible())
        self.assertIn("Installed in", widget.installDirNote.text)

    def test_developer_mode_keeps_it_editable(self):
        _QSettings.store["Developer/DeveloperMode"] = True
        widget = self.folder(fixtures.native(), backends.NATIVE)
        self.assertTrue(widget.installDirEdit.isVisible())


class TestPortTaken(PanelTestCase):
    def test_the_panel_offers_another_port_and_starts_there(self):
        status = fixtures.native(container={"running": False}, server={"healthy": False})
        SlicerCloud.LocalServerDeployment = serving(status, backends.NATIVE)
        with mock.patch.object(SlicerCloud.deploy, "port_in_use", return_value=True), \
                mock.patch.object(SlicerCloud.deploy, "find_free_port", return_value=8003):
            widget = self.panel()
            self.assertEqual(visible(widget) & {"portButton", "installButton"}, {"portButton"})
            self.assertIn("another program", widget._statusRows["port"].text)
            widget.onTryAnotherPort()
        self.assertIn(("up", 8003, "127.0.0.1"), widget.deployment().calls)

    def test_no_free_port_is_an_error_not_a_job(self):
        status = fixtures.native(container={"running": False}, server={"healthy": False})
        SlicerCloud.LocalServerDeployment = serving(status, backends.NATIVE)
        with mock.patch.object(SlicerCloud.deploy, "find_free_port", return_value=None):
            widget = self.panel()
            widget.onTryAnotherPort()
        self.assertEqual(len(DIALOGS["error"]), 1)
        self.assertEqual(widget.deployment().calls, [])


class TestReplacingAServer(PanelTestCase):
    """A lab server a user already depends on must not be overwritten by pressing
    one button: explicit confirmation, then replacement, and a way back."""

    def setUp(self):
        PanelTestCase.setUp(self)
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)
        client.configure(server_url=REMOTE_URL, token="lab-token")
        SlicerCloud.LocalServerDeployment = serving(
            fixtures.docker(server={"public_url": PUBLIC_URL}))

    def test_it_asks_naming_the_server_in_use(self):
        widget = self.panel()
        widget.onUseHere()
        (question,) = DIALOGS["confirm"]
        self.assertIn(REMOTE_URL, question)
        self.assertIn("Switch back", question)

    def test_yes_replaces_it_and_keeps_the_previous_one(self):
        widget = self.panel()
        widget.onUseHere()
        self.assertEqual(SlicerCloud.get_client().server_url, PUBLIC_URL)
        self.assertEqual(SlicerCloud.get_client().token, "the-token")
        self.assertEqual(_QSettings.store["SlicerCloud/PreviousServerUrl"], REMOTE_URL)
        self.assertEqual(_QSettings.store["SlicerCloud/PreviousServerToken"], "lab-token")

    def test_no_changes_nothing_and_runs_no_job(self):
        ANSWER["yes"] = False
        widget = self.panel()
        widget.onUseHere()
        self.assertEqual(SlicerCloud.get_client().server_url, REMOTE_URL)
        self.assertNotIn("SlicerCloud/PreviousServerUrl", _QSettings.store)
        self.assertIn("nothing was changed", "\n".join(widget.logView.lines).lower())

    def test_install_asks_too_and_no_means_no_job(self):
        ANSWER["yes"] = False
        SlicerCloud.LocalServerDeployment = serving(fixtures.docker_uninstalled())
        widget = self.panel()
        widget.onInstall()
        self.assertEqual(len(DIALOGS["confirm"]), 1)
        self.assertEqual(widget.deployment().calls, [])

    def test_switch_back_restores_the_previous_server(self):
        widget = self.panel()
        widget.onUseHere()
        self.assertIn("switchBackButton", visible(widget))
        widget.onSwitchBack()
        self.assertEqual(SlicerCloud.get_client().server_url, REMOTE_URL)
        self.assertEqual(SlicerCloud.get_client().token, "lab-token")
        self.assertEqual(_QSettings.store["ServerTools/ServerUrl"], REMOTE_URL)
        self.assertNotIn("switchBackButton", visible(widget))

    def test_nothing_is_asked_when_no_server_is_set_or_it_is_this_computer(self):
        client = SlicerCloud.get_client()
        for url in ("https://set-your-server.invalid", "https://127.0.0.1:9000"):
            client.configure(server_url=url, token="")
            _reset_dialogs()
            self.panel().onUseHere()
            self.assertEqual(DIALOGS["confirm"], [], url)
        self.assertNotIn("SlicerCloud/PreviousServerUrl", _QSettings.store)

    def test_already_pointing_here_asks_nothing(self):
        SlicerCloud.get_client().configure(server_url=PUBLIC_URL, token="x")
        self.panel().onUseHere()
        self.assertEqual(DIALOGS["confirm"], [])

    def test_a_server_with_no_usable_address_is_not_saved(self):
        SlicerCloud.LocalServerDeployment = serving(fixtures.docker())
        widget = self.panel()
        widget.onUseHere()
        self.assertEqual(SlicerCloud.get_client().server_url, REMOTE_URL)
        self.assertTrue(any("no public address" in text for text in DIALOGS["warning"]))


class TestDeveloperMode(PanelTestCase):
    """Advanced fields count only with Slicer's own developer mode: outside it
    they are ignored, not just hidden, even when saved from an earlier session."""

    FIELDS = {
        "SlicerCloud/RepoUrl": "https://example.org/fork/VISOR-serve.git",
        "SlicerCloud/Branch": "feat/test",
        "SlicerCloud/ToolsRepoUrl": "https://example.org/fork/SADT-VISOR.git",
        "SlicerCloud/ToolsBranch": "feat/tools-test",
        "SlicerCloud/Port": 8123,
        "SlicerCloud/BindAddress": "",
    }

    def filled(self, developer):
        _QSettings.store.update(self.FIELDS)
        if developer:
            _QSettings.store["Developer/DeveloperMode"] = True
        return self.panel()

    def test_outside_developer_mode_the_saved_fields_decide_nothing(self):
        from SlicerCloudLib import deploy
        d = self.filled(False).deployment()
        self.assertEqual((d.repo_url, d.branch), (deploy.DEFAULT_REPO_URL, deploy.DEFAULT_BRANCH))
        self.assertEqual((d.tools_repo_url, d.tools_branch),
                         (deploy.DEFAULT_TOOLS_REPO_URL, deploy.DEFAULT_TOOLS_BRANCH))

    def test_outside_developer_mode_even_a_value_typed_now_is_ignored(self):
        from SlicerCloudLib import deploy
        widget = self.filled(False)
        widget.branchEdit.text = "typed-just-now"
        widget.toolsBranchEdit.text = "typed-too"
        d = widget.deployment()
        self.assertEqual((d.branch, d.tools_branch), (deploy.DEFAULT_BRANCH, deploy.DEFAULT_TOOLS_BRANCH))

    def test_inside_developer_mode_they_are_used(self):
        d = self.filled(True).deployment()
        self.assertEqual(d.repo_url, self.FIELDS["SlicerCloud/RepoUrl"])
        self.assertEqual(d.branch, "feat/test")
        self.assertEqual(d.tools_repo_url, self.FIELDS["SlicerCloud/ToolsRepoUrl"])
        self.assertEqual(d.tools_branch, "feat/tools-test")

    def test_the_port_and_the_network_choice_are_ignored_outside_it(self):
        widget = self.filled(False)
        self.assertIsNone(widget.selectedPort())
        self.assertEqual(widget.selectedBind(), "127.0.0.1")
        inside = self.filled(True)
        self.assertEqual(inside.selectedPort(), 8123)

    def test_the_advanced_box_is_shown_in_developer_mode_only(self):
        self.assertFalse(self.panel()._advanced.isVisible())
        _QSettings.store["Developer/DeveloperMode"] = True
        self.assertTrue(self.panel()._advanced.isVisible())

    def test_the_tools_rows_exist_only_for_a_backend_that_has_tools(self):
        _QSettings.store["Developer/DeveloperMode"] = True
        docker = self.panel()
        self.assertFalse(docker.toolsBranchEdit in [w for w in docker._toolsRows if w.isVisible()])
        SlicerCloud.LocalServerDeployment = NativeFake
        native = self.panel()
        self.assertTrue(all(w.isVisible() for w in native._toolsRows))
        self.assertFalse(any(w.isVisible() for w in native._bindRows))

    def test_switching_developer_mode_while_the_panel_is_open_takes_effect(self):
        from SlicerCloudLib import deploy
        widget = self.panel()
        widget.branchEdit.text = "feat/test"
        self.assertEqual(widget.deployment().branch, deploy.DEFAULT_BRANCH)
        _QSettings.store["Developer/DeveloperMode"] = True
        widget.enter()
        self.assertEqual(widget.deployment().branch, "feat/test")
        self.assertTrue(widget._advanced.isVisible())
        _QSettings.store["Developer/DeveloperMode"] = False
        widget.enter()
        self.assertEqual(widget.deployment().branch, deploy.DEFAULT_BRANCH)
        self.assertFalse(widget._advanced.isVisible())

    def test_install_outside_developer_mode_sends_no_port_and_loopback(self):
        SlicerCloud.LocalServerDeployment = NativeFake
        widget = self.filled(False)
        widget.onInstall()
        self.assertEqual(widget.deployment().calls[-1], ("up", None, "127.0.0.1"))

    def test_install_in_developer_mode_sends_the_port_chosen(self):
        SlicerCloud.LocalServerDeployment = NativeFake
        widget = self.filled(True)
        widget.onInstall()
        self.assertEqual(widget.deployment().calls[-1], ("up", 8123, "127.0.0.1"))

    def test_the_point_every_module_choice_counts_only_in_developer_mode(self):
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)
        client.configure(server_url="https://set-your-server.invalid", token="")
        SlicerCloud.LocalServerDeployment = serving(
            fixtures.docker_uninstalled(),
            up=lambda self, progress_cb=None, force_recreate=False, port=None, bind=None: {
                "url": LOCAL_URL, "token": "t", "healthy": True, "public_url": PUBLIC_URL})
        widget = self.panel()
        widget.useServerCheck.checked = False
        widget.onInstall()
        self.assertEqual(client.server_url, PUBLIC_URL)
        _QSettings.store["Developer/DeveloperMode"] = True
        client.configure(server_url="https://set-your-server.invalid", token="")
        widget.onInstall()
        self.assertEqual(client.server_url, "https://set-your-server.invalid")

    def test_a_saved_bind_survives_a_session_outside_developer_mode(self):
        """Saving must keep what the combo holds, not the loopback it is forced to."""
        _QSettings.store["SlicerCloud/BindAddress"] = ""
        widget = self.panel()
        widget._saveSettings()
        self.assertEqual(_QSettings.store["SlicerCloud/BindAddress"], "")


if __name__ == "__main__":
    unittest.main()


SWITCH = SlicerCloud.deploy.BranchSwitch


class BranchSwitchCase(PanelTestCase):
    """The panel used to put an existing clone on the form's branch without a word."""

    def recording(self, switches, update_result=None):
        outer = self
        update_result = update_result or {"clone": {}, "pulled": False, "recreated": False}

        class Recording(FakeDeployment):
            def __init__(self, *a, **k):
                FakeDeployment.__init__(self, *a, **k)
                self.switches = list(switches)
                outer.deployed = self

            def update(self, progress_cb=None, force=False, bind=None):
                self.calls.append("update")
                self.after = {sw.path: sw.wanted for sw in self.switches}
                return dict(update_result)

            def up(self, progress_cb=None, force_recreate=False, port=None, bind=None):
                self.calls.append("up")
                self.after = {sw.path: sw.wanted for sw in self.switches}
                return {"url": "https://x", "healthy": True}

        SlicerCloud.LocalServerDeployment = Recording

    def logged(self, widget):
        return "\n".join(widget.logView.lines)


class TestBranchSwitchIsAsked(BranchSwitchCase):
    switch = SWITCH("server repository", "/s", "feat/work", "main", False)

    def test_install_asks_naming_both_branches_then_reports_the_move(self):
        self.recording([self.switch])
        widget = self.panel()
        widget.onInstall()
        (question,) = DIALOGS["confirm"]
        self.assertIn("feat/work", question)
        self.assertIn("main", question)
        self.assertIn("up", widget.deployment().calls)
        self.assertIn("passed from 'feat/work' to 'main'", self.logged(widget))

    def test_no_means_no_job_and_no_switch(self):
        ANSWER["yes"] = False
        self.recording([self.switch])
        widget = self.panel()
        widget.onInstall()
        widget.onUpdate()
        self.assertEqual(len(DIALOGS["confirm"]), 2)
        self.assertEqual(widget.deployment().calls, [])
        self.assertIn("nothing was changed", self.logged(widget).lower())

    def test_update_asks_too(self):
        self.recording([self.switch])
        widget = self.panel()
        widget.onUpdate()
        self.assertEqual(len(DIALOGS["confirm"]), 1)
        self.assertIn("update", widget.deployment().calls)
        self.assertIn("passed from 'feat/work' to 'main'", self.logged(widget))

    def test_a_dirty_tree_is_refused_without_asking(self):
        self.recording([self.switch._replace(dirty=True)])
        widget = self.panel()
        widget.onInstall()
        widget.onUpdate()
        self.assertEqual(DIALOGS["confirm"], [])
        self.assertEqual(len(DIALOGS["error"]), 2)
        self.assertIn("uncommitted", DIALOGS["error"][0])
        self.assertIn("feat/work", DIALOGS["error"][0])
        self.assertEqual(widget.deployment().calls, [])

    def test_no_mismatch_means_no_question(self):
        self.recording([])
        widget = self.panel()
        widget.onInstall()
        widget.onUpdate()
        self.assertEqual(DIALOGS["confirm"], [])

    def test_the_question_names_every_clone_that_would_move(self):
        tools = SWITCH("tools repository", "/t", "feat/mps", "main", False)
        self.recording([self.switch, tools])
        widget = self.panel()
        widget.onUpdate()
        question = DIALOGS["confirm"][0]
        self.assertIn("server repository", question)
        self.assertIn("tools repository", question)
        self.assertIn("feat/mps", question)


class TestAlreadyRunning(PanelTestCase):
    def install(self, reported):
        class Fake(NativeFake):
            pass
        Fake.reported = reported
        SlicerCloud.LocalServerDeployment = Fake
        widget = self.panel()
        widget.useServerCheck.checked = True
        widget.onInstall()
        return widget

    def setUp(self):
        PanelTestCase.setUp(self)
        import tempfile
        import shutil
        from ServerToolsCoreLib import tls
        work = tempfile.mkdtemp(prefix="panel_ar_")
        self.addCleanup(shutil.rmtree, work, True)
        mock.patch.object(tls, "local_tls_dir", return_value=os.path.join(work, "tls")).start()
        mock.patch.object(SlicerCloud.deploy, "is_apple_silicon", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        from SlicerCloudLib import local_cert
        local_cert.ensure_local_certificate(os.path.join(work, "tls"))
        client = SlicerCloud.get_client()
        self.addCleanup(client.configure, server_url=client.server_url, token=client.token)

    base = {"url": "https://127.0.0.1:8000", "token": "t", "healthy": True, "tls": {"enabled": True}}

    def test_an_already_running_server_is_said_to_be_that(self):
        widget = self.install(dict(self.base, already_running=True, restarted=False))
        self.assertIn("Server already running, address and token registered", "\n".join(widget.logView.lines))
        self.assertEqual(SlicerCloud.get_client().server_url, "https://127.0.0.1:8000")
        self.assertEqual(DIALOGS["warning"], [])

    def test_a_restart_is_not_called_already_running(self):
        widget = self.install(dict(self.base, already_running=False, restarted=True))
        self.assertNotIn("already running", "\n".join(widget.logView.lines))

    def test_the_field_may_be_absent(self):
        widget = self.install(dict(self.base))
        self.assertNotIn("already running", "\n".join(widget.logView.lines))
        self.assertEqual(SlicerCloud.get_client().server_url, "https://127.0.0.1:8000")


class TestNativeSupportIsCheckedFirst(PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        mock.patch.object(SlicerCloud.deploy, "is_apple_silicon", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        SlicerCloud.LocalServerDeployment = NativeFake

    def test_install_checks_before_cloning(self):
        widget = self.panel()
        widget.onInstall()
        calls = widget.deployment().calls
        self.assertLess(calls.index("require_native_support"), calls.index("clone"))

    def test_update_checks_before_updating(self):
        class Upd(NativeFake):
            def update(self, progress_cb=None, force=False, bind=None):
                self.calls.append("update")
                return {"clone": {}}
        SlicerCloud.LocalServerDeployment = Upd
        widget = self.panel()
        widget.onUpdate()
        calls = widget.deployment().calls
        self.assertLess(calls.index("require_native_support"), calls.index("update"))

    def test_a_refusal_is_shown_and_nothing_else_runs(self):
        class Refusing(NativeFake):
            def require_native_support(self, progress_cb=None):
                raise SlicerCloud.DeploymentError("Branch 'x' of y has no native support")
        SlicerCloud.LocalServerDeployment = Refusing
        widget = self.panel()
        widget.onInstall()
        self.assertIn("no native support", DIALOGS["error"][0])
        self.assertEqual(widget.deployment().calls, [])
