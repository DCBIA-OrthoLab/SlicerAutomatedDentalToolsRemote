"""Slicer Cloud — stand a tool server up from inside Slicer, and keep it current.

Every other module in this extension is a thin GUI over a tool the server
already exposes; this one is the GUI over the *server itself*. It clones the
server repository, checks docker, starts the container, tells you when the
clone has fallen behind and relaunches it, and lets you choose which tools'
model bundles land on disk — the manifest is ~29 GB and nobody uses all of it.

The logic lives in the server repository's `scripts/server_ctl.py`, driven
through `SlicerCloudLib.deploy`. This file is the panel: widgets, threading,
and turning a status dict into something readable. See ARCHITECTURE.md.
"""

import getpass
import logging
from urllib.parse import urlsplit

import ctk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule, ScriptedLoadableModuleWidget

from ServerToolsCoreLib import design, get_client
from ServerToolsCoreLib.client import checked_server_url
from ServerToolsCoreLib.settings_qt import notify_uploads_are_not_deidentified, save_overrides
from ServerToolsCoreLib.errors import plain_text
from ServerToolsCoreLib.worker import BackgroundJob
from SlicerCloudLib import backends, deploy
from SlicerCloudLib import state as serverState
from SlicerCloudLib.deploy import (
    DEFAULT_BRANCH,
    DEFAULT_INSTALL_DIR,
    DEFAULT_REPO_URL,
    DEFAULT_SERVER_URL,
    DEFAULT_TOOLS_BRANCH,
    DEFAULT_TOOLS_REPO_URL,
    DeploymentError,
    LocalServerDeployment,
)

logger = logging.getLogger("SlicerCloud")

_SETTINGS_GROUP = "SlicerCloud"
_KEY_INSTALL_DIR = f"{_SETTINGS_GROUP}/InstallDir"
_KEY_REPO_URL = f"{_SETTINGS_GROUP}/RepoUrl"
_KEY_BRANCH = f"{_SETTINGS_GROUP}/Branch"
_KEY_TOOLS_BRANCH = f"{_SETTINGS_GROUP}/ToolsBranch"
_KEY_TOOLS_REPO_URL = f"{_SETTINGS_GROUP}/ToolsRepoUrl"
_KEY_SELECTED_TOOLS = f"{_SETTINGS_GROUP}/SelectedTools"
_KEY_PORT = f"{_SETTINGS_GROUP}/Port"
_KEY_BIND = f"{_SETTINGS_GROUP}/BindAddress"
_KEY_STOP_ON_EXIT = f"{_SETTINGS_GROUP}/StopOnExit"
# The server a module was pointed at before this panel replaced it, so that
# "Switch back" can restore it. Same sensitivity as ServerTools/ApiToken, which
# already sits in the same settings file.
_KEY_PREVIOUS_URL = f"{_SETTINGS_GROUP}/PreviousServerUrl"
_KEY_PREVIOUS_TOKEN = f"{_SETTINGS_GROUP}/PreviousServerToken"
# Slicer's own switch (the one that shows Reload & Test): what makes the fields
# below it count.
_KEY_DEVELOPER_MODE = "Developer/DeveloperMode"

_DEFAULT_PORT = 8000
# On by default: the panel manages a server on the user's OWN machine, and a
# container nobody asked to keep is a background process on a clinician's
# laptop. `stop` (never `down`) is what makes this cheap — see the checkbox
# tooltip and stopServerOnExit().
_DEFAULT_STOP_ON_EXIT = True

# The log pane is a ring buffer, not an archive: a `docker compose up` on a
# fresh host prints tens of thousands of layer-progress lines, and an
# unbounded QTextEdit makes the panel crawl long before anyone reads them.
_LOG_MAX_BLOCKS = 2000

# Every row either backend can show, in an order that keeps each backend's own
# order. The backend decides which are shown and what they say (checks()); the
# rest are hidden, so no row ever describes something this machine does not use.
_ROW_KEYS = ("git", "docker", "compose", "uv", "gpu", "disk", "port", "clone", "container", "health")


def useSlicerStartupEnvironment() -> None:
    """Hand every subprocess the environment Slicer started from.

    Slicer's launcher exports its own PYTHONHOME/PYTHONPATH, so the SYSTEM
    python3 that runs `server_ctl.py` would start against SLICER's standard
    library — measured on a fresh install as python3.10 loading Slicer's
    python3.12 stdlib and dying on "SRE module mismatch" at `import argparse`,
    with exit code 1 and an empty stdout. Nothing in the panel worked.

    Called from the module class, so it is in force before the panel — or the
    quit hook — can spawn anything.
    """
    try:
        deploy.set_subprocess_env(slicer.util.startupEnvironment())
    except Exception:  # noqa: BLE001 - inheriting is better than not loading
        logger.exception("Could not read Slicer's startup environment; "
                         "subprocesses will inherit Slicer's own instead")


def _qt_get(obj, name):
    """Read a Qt getter that PythonQt may have turned into a property.

    PythonQt collapses a `foo()` / `setFoo()` pair into an attribute, while
    PyQt (and every stub written from the C++ docs) keeps `foo()` a method.
    `QTextEdit.document` is one of those, and getting it wrong is not a
    cosmetic difference: it raised inside `setup()`, so the panel was left
    half-built and every later click died on a missing attribute.
    """
    value = getattr(obj, name)
    return value() if callable(value) else value


def stopServerOnExit() -> None:
    """Stop the managed container as Slicer quits, if the user asked for that.

    Wired from the module class rather than from the widget, and that is the
    whole reason it is a module-level function: a widget only exists once
    someone has *opened* the panel, so a server started last Monday and never
    revisited would never be stopped — which is precisely the case this
    setting is for.

    `stop`, never `down`: the container is kept, so its writable layer keeps
    the `pip install --user` the image's command performed. Dependencies are
    therefore installed once, on the first start, and a later start is a few
    seconds. Removing the container is what would make them reinstall.

    Everything is swallowed. This runs while Slicer is tearing itself down;
    there is no window left to show an error in, and an exception raised in an
    `aboutToQuit` handler is a crash on exit for a background convenience.
    """
    try:
        settings = qt.QSettings()
        raw = settings.value(_KEY_STOP_ON_EXIT)
        enabled = _DEFAULT_STOP_ON_EXIT if raw is None else str(raw).lower() in ("true", "1")
        if not enabled:
            return
        install_dir = str(settings.value(_KEY_INSTALL_DIR) or DEFAULT_INSTALL_DIR)
        deployment = LocalServerDeployment(install_dir)
        if deployment.stop_detached():
            logger.info("Slicer is quitting: asked docker to stop the server in %s", install_dir)
    except Exception:  # noqa: BLE001 - see the docstring: never break the quit path
        logger.exception("Could not stop the server on exit")


def _dockerGroupInstructions(installed_just_now: bool = False) -> str:
    """The screen a first-time user is most likely to be stopped by.

    Docker is installed and its daemon is running; this account is simply not in
    the `docker` group. Nothing the panel can do fixes it: adding a user to a
    group needs root, and the new session has to be the user's own — so the
    panel's entire job here is to be followable without a second window open.

    Numbered steps rather than prose, because that is what people act on, and
    because each of these three was watched to fail on its own: the terminal
    shortcut is named (nobody should have to go hunting for a terminal), the
    PASTE shortcut is named (Ctrl+V does not paste into a terminal, so a copied
    command silently goes nowhere), and the invisible sudo password is called
    out (a prompt that echoes nothing reads as a frozen terminal).
    """
    opening = _(
        "Docker was installed successfully. One step is left, and it cannot be done from "
        "here: your account has to join the 'docker' group, which only takes effect in a "
        "new login session."
    ) if installed_just_now else _(
        "Docker is installed and running, but this account is not allowed to use it yet."
    )
    return opening + "\n\n" + _(
        "Do this once:\n"
        "\n"
        "1. Open a terminal — press Ctrl+Alt+T.\n"
        "\n"
        "2. Copy this command, exactly:\n"
        "\n"
        "       sudo usermod -aG docker {0}\n"
        "\n"
        "3. Paste it into the terminal with Ctrl+Shift+V, then press Enter.\n"
        "   (Ctrl+V does NOT paste into a terminal.)\n"
        "\n"
        "4. Type your password and press Enter. Nothing appears on screen while\n"
        "   you type — that is normal, keep typing.\n"
        "\n"
        "5. Log out and log back in, or restart the computer. This step cannot be\n"
        "   skipped: a group only applies to a NEW session.\n"
        "\n"
        "6. Start Slicer again, open this panel, and press 'Install and start'."
    ).format(getpass.getuser())


_human = backends.human_size


class SlicerCloud(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Slicer Cloud")
        # Beside the other two panels nobody opens to treat a patient:
        # ServerToolsCore and ServerToolsSettings. This one deploys and updates
        # the server, which is done once per machine rather than once per
        # cohort, and a clinician scanning the module list for a tool should
        # not have to step over it.
        self.parent.categories = ["Automated Dental Tools Remote.Advanced"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = ["Automated Dental Tools team"]
        self.parent.helpText = _("""
        Deploy and maintain the Automated Dental Tools inference server on this machine, or on
        one you can reach a terminal on. It clones the server repository, checks that Docker is
        installed and usable, starts the container, and points every module of this extension at
        it. <b>Update server</b> checks whether the clone has fallen behind its remote and
        relaunches the container when it has.
        <br><br>
        Model bundles are downloaded per tool rather than all at once — the full set is about
        29 GB. Tick only the tools you use; come back later to add another one and only that one
        is downloaded.
        See more information in <a href="https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalTools">documentation</a>.
        """)
        self.parent.acknowledgementText = _("This module was supported by NIDCR R01 024450.")

        # Both run once, when Slicer discovers this module at startup — before
        # the panel exists, which is what they are for.
        useSlicerStartupEnvironment()

        # Runs once, when Slicer discovers this module at startup — before the
        # panel has ever been opened, which is the point: the server may have
        # been started in an earlier session and never revisited. Same place
        # ServerToolsCore applies its saved settings, for the same reason.
        slicer.app.connect("aboutToQuit()", stopServerOnExit)


class SlicerCloudWidget(ScriptedLoadableModuleWidget):
    """The panel. One background job at a time, always cancellable."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.uiWidget = None
        self._deployment = None
        self._job = None
        self._jobName = ""
        self._statusRows = {}
        self._toolRows = {}          # {tool name: (QCheckBox, QLabel)}
        self._catalog = None
        self._registeredTools = None  # tool names the running server exposes, None if unknown
        self._lastStatus = None
        self._jobBlocking = True      # False while a passive status check runs
        self._loadingSettings = False  # True while _loadSettings writes the widgets
        self._buildFailed = False      # setup() could not build the panel
        self._lastFacts = {}           # what only this machine can say: disk, port
        self._jobKind = None           # "install", "start" or "work" while a job runs

    # ------------------------------------------------------------------
    # Slicer lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        self.uiWidget = qt.QWidget()
        self.layout.addWidget(self.uiWidget)
        root = qt.QVBoxLayout(self.uiWidget)

        self._statusBadge = design.status_badge()
        root.addWidget(self._statusBadge)

        # Created BEFORE anything that can fail, because `_setBusy` touches
        # both and every job path goes through it. When a widget below raised,
        # these two did not exist and each later click died on an
        # AttributeError instead of on the real problem.
        self._progressLabel = design.progress_label()
        self.cancelButton = design.danger_button(_("Cancel"))
        self.cancelButton.setVisible(False)
        self.cancelButton.clicked.connect(self.onCancel)

        # One bad widget must not cost the whole panel. ServerToolWidgetBase
        # already wraps its own build for exactly this reason (ARCHITECTURE.md,
        # "Schema fetch is synchronous"); this one did not, and a single
        # PythonQt property mismatch turned the module into a dead screen with
        # nothing on it explaining why.
        try:
            root.addWidget(self._buildServerBox())
            root.addWidget(self._buildDataBox())
            root.addWidget(self._buildLogBox())
        except Exception as exc:  # noqa: BLE001 - reported on screen, see above
            logger.exception("Could not build the Slicer Cloud panel")
            self._buildFailed = True
            root.addWidget(design.warning_label(_(
                "The Slicer Cloud panel could not be built: {0}\n\n"
                "This is a bug — please report it with the Python console output. "
                "The server itself is unaffected; it can be driven from a terminal with "
                "python3 scripts/server_ctl.py in the install folder."
            ).format(exc)))

        root.addWidget(self._progressLabel)
        root.addWidget(self.cancelButton)
        root.addStretch(1)
        design.apply(self.uiWidget)

        if self._buildFailed:
            return
        self._loadSettings()
        self.onRefresh()

    def cleanup(self) -> None:
        # Only on teardown. Leaving the module (`exit`) deliberately does
        # NOT cancel: an install or a download is minutes to hours long, and
        # looking at another module is not a reason to abandon it half-done.
        self._cancelJob()

    def enter(self) -> None:
        if self._buildFailed:
            return
        design.apply(self.uiWidget)
        # Cheap and offline: a status refresh never fetches from the remote, so
        # coming back to the panel cannot hang on a machine with no network.
        # Skipped only while real work runs, so re-entering the module during
        # an install neither disturbs it nor pops a dialog.
        if not self._busyWithWork():
            self.onRefresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _buildServerBox(self):
        box = ctk.ctkCollapsibleButton()
        box.text = _("Local server")
        self._serverBox = box
        layout = qt.QVBoxLayout(box)

        form = qt.QFormLayout()
        layout.addLayout(form)

        self.installDirEdit = ctk.ctkPathLineEdit()
        self.installDirEdit.filters = ctk.ctkPathLineEdit.Dirs
        self._installDirTitle = design.section_title(_("Install folder"))
        form.addRow(self._installDirTitle, self.installDirEdit)
        # Once installed the folder is only stated: it holds the server and its
        # tools, moving it would leave the old copy behind.
        self.installDirNote = design.hint_label("")
        layout.addWidget(self.installDirNote)

        # Developer mode only, and IGNORED outside it (deployment(), selectedBind(),
        # selectedPort()): hiding a field is not enough when its saved value still
        # decides what gets cloned.
        advanced = ctk.ctkCollapsibleButton()
        advanced.text = _("Advanced (developer mode)")
        advanced.collapsed = True
        self._advanced = advanced
        advancedForm = qt.QFormLayout(advanced)
        self._toolsRows = []     # shown only for a backend that has a tools checkout
        self._bindRows = []      # shown only for a backend that lets the user choose

        def addRow(text, widget, group=None):
            title = design.section_title(text)
            advancedForm.addRow(title, widget)
            if group is not None:
                group.append(title)
                group.append(widget)

        self.repoUrlEdit = qt.QLineEdit()
        self.repoUrlEdit.setToolTip(_("Clone a fork instead of the reference repository."))
        addRow(_("Repository URL"), self.repoUrlEdit)
        self.branchEdit = qt.QLineEdit()
        addRow(_("Branch"), self.branchEdit)
        self.toolsRepoUrlEdit = qt.QLineEdit()
        self.toolsRepoUrlEdit.setToolTip(_("The tools repository to clone."))
        addRow(_("Tools repository URL"), self.toolsRepoUrlEdit, self._toolsRows)
        self.toolsBranchEdit = qt.QLineEdit()
        self.toolsBranchEdit.setToolTip(_("The branch of the tools repository to use."))
        addRow(_("Tools branch"), self.toolsBranchEdit, self._toolsRows)
        self.portSpin = qt.QSpinBox()
        self.portSpin.setRange(1024, 65535)
        self.portSpin.setValue(_DEFAULT_PORT)
        self.portSpin.setToolTip(
            _("Host port the server is published on. Change it only if something else already "
              "holds 8000: it is remembered, so it only has to be set once.")
        )
        addRow(_("Port"), self.portSpin)

        # Populated from the machine's real interfaces rather than typed: an
        # address is exactly the kind of thing nobody remembers and everybody
        # mistypes, and a wrong one makes the container fail to start with a
        # docker error about a bind, not a sentence anyone can act on.
        self.bindCombo = qt.QComboBox()
        # The risk level is kept beside the combo rather than in Qt item data:
        # one lookup either way, and it does not depend on which item-data roles
        # a given Qt binding exposes.
        self._bindKinds = {}
        for address, label, kind in deploy.host_addresses():
            self.bindCombo.addItem(_("{0}  ({1})").format(label, address or _("all addresses")),
                                   address)
            self._bindKinds[address] = kind
        addRow(_("Network access"), self.bindCombo, self._bindRows)
        self.bindCombo.currentIndexChanged.connect(self._onBindChanged)

        self.useServerCheck = qt.QCheckBox(
            _("Point every Automated Dental Tools module at this server once it is up")
        )
        self.useServerCheck.setChecked(True)
        self.useServerCheck.setToolTip(
            _("Saves this server's address and API key as the extension's settings: the same "
              "thing the Server Tools Settings module edits. Outside developer mode this "
              "always happens.")
        )
        advancedForm.addRow(self.useServerCheck)
        layout.addWidget(advanced)

        checks = qt.QGridLayout()
        checks.setColumnStretch(1, 1)
        layout.addLayout(checks)
        self._statusNames = {}
        for row, key in enumerate(_ROW_KEYS):
            name = design.section_title(key)
            value = qt.QLabel(_("checking..."))
            value.setWordWrap(True)
            checks.addWidget(name, row, 0)
            checks.addWidget(value, row, 1)
            self._statusNames[key] = name
            self._statusRows[key] = value

        self.hintLabel = design.hint_label("")
        layout.addWidget(self.hintLabel)

        # Which of these is on screen is the server's state (_applyActions): the
        # others are hidden, not greyed. The install button is one button whose
        # text follows the state: install, start, or the fix for what is missing.
        buttons = qt.QHBoxLayout()
        layout.addLayout(buttons)
        self.installButton = design.primary_button(_("Install and start"))
        self.updateButton = design.primary_button(_("Update server"))
        self.useButton = design.primary_button(_("Use this server"))
        self.portButton = design.primary_button(_("Try another port"))
        self.stopButton = design.danger_button(_("Stop"))
        for button in (self.installButton, self.updateButton, self.useButton, self.portButton,
                       self.stopButton):
            buttons.addWidget(button)

        secondary = qt.QHBoxLayout()
        layout.addLayout(secondary)
        self.refreshButton = design.link_button(_("Refresh"))
        self.checkUpdateButton = design.link_button(_("Check for updates"))
        self.checkUpdateButton.setToolTip(_("Fetch the remote to see whether the clone is behind."))
        self.showLogsButton = design.link_button(_("Server logs"))
        self.switchBackButton = design.link_button(_("Switch back"))
        self.switchBackButton.setToolTip(_(
            "Point every module at the server they used before this one."))
        self.gpuButton = design.link_button(_("Enable GPU support"))
        self.gpuButton.setToolTip(_(
            "This machine has an NVIDIA card that Docker cannot reach, so the server would "
            "run on the CPU. Installs the NVIDIA Container Toolkit if needed and registers it "
            "with Docker. Needs your password, and restarts Docker."
        ))
        for button in (self.refreshButton, self.checkUpdateButton, self.showLogsButton,
                       self.switchBackButton, self.gpuButton):
            secondary.addWidget(button)
        secondary.addStretch(1)
        self.gpuButton.setVisible(False)
        self.switchBackButton.setVisible(False)

        self.stopOnExitCheck = qt.QCheckBox(_("Stop the server when Slicer closes"))
        self.stopOnExitCheck.toggled.connect(lambda _checked: self._saveSettings())
        layout.addWidget(self.stopOnExitCheck)

        self.installButton.clicked.connect(lambda: self.onInstall())
        self.updateButton.clicked.connect(self.onUpdate)
        self.stopButton.clicked.connect(self.onStop)
        self.useButton.clicked.connect(self.onUseHere)
        self.portButton.clicked.connect(self.onTryAnotherPort)
        self.switchBackButton.clicked.connect(self.onSwitchBack)
        # Wrapped rather than connected directly: `clicked` carries a `checked`
        # bool, and onRefresh's first parameter is `check_remote`: a direct
        # connection would let a widget detail decide whether Refresh hits the
        # network.
        self.refreshButton.clicked.connect(lambda: self.onRefresh())
        self.checkUpdateButton.clicked.connect(lambda: self.onRefresh(check_remote=True))
        self.showLogsButton.clicked.connect(self.onShowLogs)
        # Wrapped for the same reason as refreshButton above: `clicked` carries a
        # `checked` bool, and _offerGpuSetup takes no argument.
        self.gpuButton.clicked.connect(lambda: self._offerGpuSetup())
        self.installDirEdit.currentPathChanged.connect(self._onInstallDirChanged)
        return box

    def _buildDataBox(self):
        box = ctk.ctkCollapsibleButton()
        box.text = _("Tool data")
        layout = qt.QVBoxLayout(box)

        layout.addWidget(design.hint_label(_(
            "Each tool needs its own model bundle on the server before it can run. Tick only "
            "what you use — the full set is about 29 GB. Anything already on disk is skipped, "
            "so coming back later to add one tool downloads only that tool."
        )))

        links = qt.QHBoxLayout()
        layout.addLayout(links)
        for label, action in (
            (_("All"), lambda: self._selectTools(lambda tool: True)),
            (_("None"), lambda: self._selectTools(lambda tool: False)),
            (_("Not downloaded"), lambda: self._selectTools(lambda tool: not tool["complete"])),
            (_("This server's tools"), self._selectRegisteredTools),
        ):
            button = design.link_button(label)
            button.clicked.connect(action)
            links.addWidget(button)
        links.addStretch(1)

        # A scroll area: fourteen tools today and the manifest only grows, and
        # this box must not push the download button off the panel.
        scroll = qt.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(design.TABS_MIN_HEIGHT)
        self._toolContainer = qt.QWidget()
        self._toolLayout = qt.QVBoxLayout(self._toolContainer)
        self._toolLayout.setSpacing(design.SPACING_XS)
        scroll.setWidget(self._toolContainer)
        layout.addWidget(scroll)

        self.selectionLabel = design.hint_label("")
        layout.addWidget(self.selectionLabel)

        self.downloadButton = design.primary_button(_("Download selected"))
        layout.addWidget(self.downloadButton)
        self.downloadButton.clicked.connect(self.onDownload)
        return box

    def _buildLogBox(self):
        box = ctk.ctkCollapsibleButton()
        box.text = _("Log")
        box.collapsed = True
        layout = qt.QVBoxLayout(box)
        self.logView = qt.QTextEdit()
        self.logView.setReadOnly(True)
        self.logView.setLineWrapMode(qt.QTextEdit.NoWrap)
        self.logView.setMinimumHeight(design.TABS_MIN_HEIGHT)
        _qt_get(self.logView, "document").setMaximumBlockCount(_LOG_MAX_BLOCKS)
        layout.addWidget(self.logView)
        clear = design.link_button(_("Clear"))
        clear.clicked.connect(lambda: self.logView.clear())
        row = qt.QHBoxLayout()
        row.addWidget(clear)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _loadSettings(self) -> None:
        # Assigning currentPath emits currentPathChanged, which is wired to
        # _onInstallDirChanged and would launch a refresh from inside setup() —
        # then setup()'s own refresh found one already running and popped a
        # modal "Something is already running" on every single module open.
        self._loadingSettings = True
        try:
            self._loadSettingsInto()
        finally:
            self._loadingSettings = False

    def _loadSettingsInto(self) -> None:
        settings = qt.QSettings()
        self.installDirEdit.currentPath = str(settings.value(_KEY_INSTALL_DIR) or DEFAULT_INSTALL_DIR)
        self.repoUrlEdit.text = str(settings.value(_KEY_REPO_URL) or DEFAULT_REPO_URL)
        self.branchEdit.text = str(settings.value(_KEY_BRANCH) or DEFAULT_BRANCH)
        self.toolsBranchEdit.text = str(settings.value(_KEY_TOOLS_BRANCH) or DEFAULT_TOOLS_BRANCH)
        self.toolsRepoUrlEdit.text = str(settings.value(_KEY_TOOLS_REPO_URL) or DEFAULT_TOOLS_REPO_URL)
        self.portSpin.value = int(settings.value(_KEY_PORT) or _DEFAULT_PORT)
        # `is None` rather than a falsy test: BIND_EVERYWHERE is the empty
        # string, so a saved "publish on every interface" must not read as
        # "never chosen" and silently fall back to loopback.
        saved = settings.value(_KEY_BIND)
        self._selectBind(deploy.BIND_LOCALHOST if saved is None else str(saved))
        raw = settings.value(_KEY_STOP_ON_EXIT)
        # `is None` rather than a falsy test: the setting is a BOOLEAN whose
        # default is True, so an unset value and a saved False must not
        # collapse to the same branch.
        self.stopOnExitCheck.checked = (
            _DEFAULT_STOP_ON_EXIT if raw is None else str(raw).lower() in ("true", "1")
        )
        self._applyBackendTexts()
        self._applyDeveloperMode()

    def _saveSettings(self) -> None:
        settings = qt.QSettings()
        settings.setValue(_KEY_INSTALL_DIR, self.installDirEdit.currentPath)
        settings.setValue(_KEY_REPO_URL, self.repoUrlEdit.text.strip())
        settings.setValue(_KEY_BRANCH, self.branchEdit.text.strip())
        settings.setValue(_KEY_TOOLS_BRANCH, self.toolsBranchEdit.text.strip())
        settings.setValue(_KEY_TOOLS_REPO_URL, self.toolsRepoUrlEdit.text.strip())
        settings.setValue(_KEY_PORT, self.portSpin.value)
        settings.setValue(_KEY_BIND, self._comboBind())
        settings.setValue(_KEY_STOP_ON_EXIT, self.stopOnExitCheck.checked)
        settings.setValue(_KEY_SELECTED_TOOLS, ",".join(sorted(self._selectedTools())))
        settings.sync()

    def _savedSelection(self):
        raw = qt.QSettings().value(_KEY_SELECTED_TOOLS)
        return {name for name in str(raw or "").split(",") if name}

    def _developerMode(self) -> bool:
        """Slicer's own developer mode (the setting that shows Reload & Test).

        Read each time, not cached: it can be switched while the panel is open.
        """
        raw = qt.QSettings().value(_KEY_DEVELOPER_MODE)
        return raw is True or str(raw).lower() in ("true", "1")

    def deployment(self) -> LocalServerDeployment:
        """Rebuilt whenever the folder/URL/branch fields change, so the object
        can never describe a different install than the one on screen.

        Outside developer mode the repository, branch, tools repository and
        tools branch fields are IGNORED, whatever they hold or were saved with:
        the compiled-in values are used. They are hidden there too, but hiding
        is not what protects a user from a branch someone typed last month.
        """
        install_dir = self.installDirEdit.currentPath or DEFAULT_INSTALL_DIR
        if self._developerMode():
            repo_url = self.repoUrlEdit.text.strip() or DEFAULT_REPO_URL
            branch = self.branchEdit.text.strip() or DEFAULT_BRANCH
            tools_branch = self.toolsBranchEdit.text.strip() or DEFAULT_TOOLS_BRANCH
            tools_repo_url = self.toolsRepoUrlEdit.text.strip() or DEFAULT_TOOLS_REPO_URL
        else:
            repo_url, branch = DEFAULT_REPO_URL, DEFAULT_BRANCH
            tools_branch, tools_repo_url = DEFAULT_TOOLS_BRANCH, DEFAULT_TOOLS_REPO_URL
        if (self._deployment is None
                or self._deployment.install_dir != install_dir
                or self._deployment.repo_url != repo_url
                or self._deployment.branch != branch
                or getattr(self._deployment, "tools_branch", tools_branch) != tools_branch
                or getattr(self._deployment, "tools_repo_url", tools_repo_url) != tools_repo_url):
            self._deployment = LocalServerDeployment(install_dir, repo_url, branch,
                                                     tools_branch=tools_branch,
                                                     tools_repo_url=tools_repo_url)
        return self._deployment

    @property
    def backend(self):
        """The backend of the current deployment: chosen once, from the platform,
        when the deployment was made. The panel never asks the platform itself."""
        return self.deployment().backend

    def selectedPort(self):
        """The port to pass to `up`, or None to leave it as the server remembers it.

        Outside developer mode the Port field is ignored: sending 8000 on every
        start would also undo a port chosen by 'Try another port', which the
        server keeps in its own .env."""
        return self.portSpin.value if self._developerMode() else None

    def _configure(self) -> bool:
        """Whether the install points every module at this server: always
        outside developer mode, the checkbox decides inside it."""
        return True if not self._developerMode() else bool(self.useServerCheck.checked)

    def _comboBind(self) -> str:
        """What the Network access combo holds, whether or not it counts."""
        data = self.bindCombo.itemData(self.bindCombo.currentIndex)
        return deploy.BIND_LOCALHOST if data is None else str(data)

    def selectedBind(self) -> str:
        """The address the server should publish on. Never None.

        A backend with a fixed bind (the native one is local only) answers for
        itself; outside developer mode the network choice is ignored and the
        answer is loopback; otherwise the combo always has loopback at worst."""
        fixed = self.backend.fixed_bind
        if fixed is not None:
            return fixed
        if not self._developerMode():
            return deploy.BIND_LOCALHOST
        return self._comboBind()

    def _selectBind(self, address: str) -> None:
        """Select a saved address, falling back to loopback when it is gone.

        An address CAN disappear between two sessions — a VPN that is down, a
        cable unplugged, DHCP handing out a different one. Falling back to
        loopback is the safe direction: the alternative is silently keeping a
        stale address, which docker refuses to bind, so the server would stop
        starting with an error about an address nobody recognises.
        """
        for index in range(self.bindCombo.count):
            if str(self.bindCombo.itemData(index)) == address:
                self.bindCombo.setCurrentIndex(index)
                return
        self.bindCombo.setCurrentIndex(0)

    def _applyBackendTexts(self) -> None:
        """The words that depend on the backend, set once the deployment (and so
        the backend) is known: the widgets are built before it is."""
        if self._buildFailed:
            return
        texts = self.backend.texts
        self._serverBox.text = _(texts.title)
        self.installDirEdit.setToolTip(_(texts.folder_note))
        self.installButton.setToolTip(_(texts.install_tooltip))
        self.updateButton.setToolTip(_(texts.update_tooltip))
        self.useButton.setText(_(texts.use_label))
        self.useButton.setToolTip(_(
            "Point every Automated Dental Tools module at this server, with its address and "
            "API key: the same thing the Server Tools Settings module edits."))
        self.stopOnExitCheck.setToolTip(_(texts.stop_on_exit_tooltip))
        self.bindCombo.setToolTip(_(texts.bind_tooltip))
        # A fixed bind is not a choice: the row is not shown at all (and the
        # combo is also disabled, so nothing can change it by accident).
        self.bindCombo.setEnabled(self.backend.fixed_bind is None)

    def _applyDeveloperMode(self) -> None:
        """Show the fields that only count in developer mode, and only the ones
        this backend has. Outside it they are hidden AND ignored (deployment())."""
        if self._buildFailed:
            return
        developer = self._developerMode()
        backend = self.backend
        self._advanced.setVisible(developer)
        for widget in self._toolsRows:
            widget.setVisible(backend.has_tools_checkout)
        for widget in self._bindRows:
            widget.setVisible(backend.fixed_bind is None)

    def _applyInstallFolder(self, current) -> None:
        """The install folder is chosen once, before the first install, with the
        free space beside it; afterwards it is stated, not offered. Developer
        mode keeps it editable."""
        editable = self._developerMode() or current.kind in (
            serverState.State.NEEDS_SETUP, serverState.State.NOT_INSTALLED)
        self.installDirEdit.setVisible(editable)
        self._installDirTitle.setVisible(editable)
        folder = self.installDirEdit.currentPath or DEFAULT_INSTALL_DIR
        if editable:
            note = _(self.backend.texts.folder_note)
            free = self._lastFacts.get("disk_free")
            if free is not None:
                note += " " + _("{0} free on that disk.").format(_human(free))
            if current.kind in (serverState.State.NEEDS_SETUP, serverState.State.NOT_INSTALLED):
                note += " " + _("Choose it now: it is fixed once the install has started.")
        else:
            note = _("Installed in {0}.").format(folder)
        self.installDirNote.setText(note)

    def _onBindChanged(self, _index=None) -> None:
        """Warn on the way OUT of loopback, once, before anything is started.

        Said here rather than at Install time because this is where the decision
        is made, and because a warning attached to the button someone presses
        five minutes later reads as an obstacle rather than as information.
        """
        if self._loadingSettings:
            return
        self._saveSettings()
        address = self.selectedBind()
        kind = self._bindKinds.get(address, "lan")
        if kind == "loopback":
            return
        if kind == "vpn":
            slicer.util.infoDisplay(plain_text(_(
                "The server will be reachable at {0} — from the machines on that private "
                "network only.\n\n"
                "This client only talks https to a publicly-signed certificate, so the "
                "other machines need one in front of the server, e.g. `tailscale serve` "
                "set up by hand, or a reverse proxy with a Let's Encrypt certificate."
            ).format(address)))
        else:
            shown = address or _("every address on this machine")
            slicer.util.warningDisplay(plain_text(_(
                "The server will be reachable at {0}.\n\n"
                "This client only talks https to a publicly-signed certificate, and the "
                "server's own listener has none.\n\n"
                "Put a publicly-signed address in front of it first -- `tailscale serve` set "
                "up by hand, or a reverse proxy with a Let's Encrypt or institutional "
                "certificate (PUBLIC_URL) -- see SECURITY.md in the server repository."
            ).format(shown)))

    def _onInstallDirChanged(self, _path=None) -> None:
        if self._loadingSettings:
            return
        self._saveSettings()
        if not self._busyWithWork():
            self.onRefresh()

    # ------------------------------------------------------------------
    # Background jobs — one at a time
    # ------------------------------------------------------------------

    def _busyWithWork(self) -> bool:
        """A real job is running — as opposed to a passive status check, which
        never blocks anything and is superseded on demand."""
        return self._job is not None and self._jobBlocking

    def _setBusy(self, busy: bool, what: str = "", disable: bool = True) -> None:
        """`disable=False` for a passive job: it narrates without locking the panel.

        Greying every button during a *status check* is what made the panel
        feel dead: the check runs on setup() and on every enter(), so the first
        thing a new user meets is an Install button they cannot press, for
        reasons nothing on screen explains.
        """
        if disable:
            for button in (self.installButton, self.updateButton, self.stopButton, self.useButton,
                           self.portButton, self.switchBackButton, self.downloadButton,
                           self.refreshButton, self.checkUpdateButton, self.showLogsButton,
                           self.gpuButton):
                button.setEnabled(not busy)
            self.cancelButton.setVisible(busy)
        self._progressLabel.setVisible(busy or bool(what))
        self._progressLabel.setText(what)

    def _startJob(self, name: str, task, on_success, refresh_on_error: bool = True,
                  blocking: bool = True, kind: str = "work") -> None:
        if self._buildFailed:
            return
        if self._job is not None:
            if self._jobBlocking:
                slicer.util.warningDisplay(
                    _("Something is already running: {0}.").format(self._jobName))
                return
            # A passive status check is always superseded rather than waited
            # for: by real work, and by a newer check (the folder just changed,
            # so the one in flight is answering about the wrong deployment).
            self._cancelJob()
        self._jobName = name
        self._jobBlocking = blocking
        self._jobKind = kind if blocking else None
        self._setBusy(True, name, disable=blocking)
        self._log(f"--- {name} ---")

        def succeeded(result):
            self._job = None
            self._setBusy(False, disable=blocking)
            self._applyActions()
            on_success(result)

        def failed(exc):
            self._job = None
            self._setBusy(False, disable=blocking)
            self._applyActions()
            self._onJobError(name, exc, refresh_on_error)

        self._job = BackgroundJob(task, on_success=succeeded, on_error=failed, on_progress=self._log)
        # After the job exists (the state reads it) and before it can finish.
        self._applyActions()
        self._job.start()

    def _cancelJob(self) -> None:
        if self._deployment is not None:
            self._deployment.cancel()
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self._setBusy(False)
        self._applyActions()

    def onCancel(self) -> None:
        self._log(_("Cancelling..."))
        self._cancelJob()
        self.onRefresh()

    def _onJobError(self, name: str, exc: Exception, refresh: bool = True) -> None:
        message = str(exc)
        self._log(f"error: {message}")
        if isinstance(exc, DeploymentError):
            # Written for the user by deploy.py or by server_ctl.py — shown verbatim.
            slicer.util.errorDisplay(plain_text(message))
        else:
            logger.exception("%s failed", name)
            slicer.util.errorDisplay(plain_text(_("{0} failed: {1}").format(name, message)))
        # `refresh` is False for the refresh job itself. Otherwise a status
        # check that fails every time — no Python 3 on the machine, say —
        # would re-launch itself from its own error handler, for ever.
        if refresh:
            self.onRefresh()

    def _log(self, message: str) -> None:
        if not message:
            return
        self.logView.append(str(message))
        self.logView.ensureCursorVisible()
        if self._job is not None:
            # The last line doubles as the progress caption: a `docker compose
            # up` is fifteen silent minutes otherwise, and a panel that shows
            # nothing reads as frozen and gets cancelled just before it works.
            self._progressLabel.setText(f"{self._jobName} — {str(message)[:120]}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def onRefresh(self, check_remote: bool = False) -> None:
        deployment = self.deployment()

        def task(progress_cb):
            status = deployment.status(check_remote=check_remote, progress_cb=progress_cb)
            catalog = deployment.catalog(progress_cb=None) if status.get("cloned") else None
            registered = None
            if status.get("server", {}).get("healthy"):
                registered = deployment.list_tools(deployment.backend.client_url(status))
            return status, catalog, registered, self._hostFacts(deployment, status)

        self._startJob(_("Checking the server"), task, self._onRefreshed,
                       refresh_on_error=False, blocking=False)

    @staticmethod
    def _hostFacts(deployment, status) -> dict:
        """What only this machine can say, for the grid and the state: free disk
        space and whether something already holds the server's port. Runs in the
        worker thread; reads no widget."""
        try:
            port = urlsplit(str((status.get("server") or {}).get("url") or "")).port
        except ValueError:
            port = None
        port = port or _DEFAULT_PORT
        running = bool((status.get("container") or {}).get("running"))
        return {
            "disk_free": deploy.free_space(deployment.install_dir),
            "port": port,
            # A server that is up holds its own port: nothing to probe.
            "port_in_use": True if running else deploy.port_in_use(port),
            "apple_silicon": bool(deployment.backend.native),
        }

    def _onRefreshed(self, result) -> None:
        status, catalog, registered, facts = result
        self._lastStatus = status
        self._lastFacts = facts
        self._registeredTools = registered
        self._renderStatus(status)
        if catalog is not None:
            self._renderCatalog(catalog)

    def confirmBranchSwitch(self, text: str) -> bool:
        """Yes/no question for a branch switch. Its own method so a test (or a
        different front end) can answer it without a dialog."""
        return bool(slicer.util.confirmYesNoDisplay(text, _("Switch branch?")))

    def _allowBranchSwitches(self, deployment) -> list:
        """Decide, before any job starts, whether the managed clones may change branch.

        Returns the switches that will happen, or None when the action must not
        run: a clone with uncommitted changes is refused outright, and the
        answer "no" leaves everything as it was. The panel used to put an
        existing clone on the form's branch without a word.
        """
        switches = deployment.pending_branch_switches()
        if not switches:
            return []
        for switch in switches:
            if switch.dirty:
                message = _(
                    "The {0} in {1} is on branch '{2}' and has uncommitted changes, so it cannot "
                    "be moved to '{3}'. Commit or discard them, or set the matching branch field "
                    "under Advanced to '{2}'. Nothing was changed."
                ).format(switch.what, switch.path, switch.current, switch.wanted)
                self._log(message)
                slicer.util.errorDisplay(plain_text(message))
                return None
        lines = "\n".join(
            _("  - {0} in {1}: '{2}' to '{3}'").format(s.what, s.path, s.current, s.wanted)
            for s in switches)
        question = _(
            "The form asks for a different branch than the one these clones are on:\n\n{0}\n\n"
            "Continuing will switch them. Switch?").format(lines)
        if not self.confirmBranchSwitch(plain_text(question)):
            self._log(_("Branch switch declined: nothing was changed."))
            return None
        return switches

    def _reportBranchSwitches(self, deployment, switches) -> None:
        for switch in switches:
            now = deployment.current_branch(switch.path)
            if now == switch.wanted:
                self._log(_("The {0} passed from '{1}' to '{2}'.").format(
                    switch.what, switch.current, switch.wanted))
            else:
                self._log(_("The {0} is on '{1}' (the form asks for '{2}').").format(
                    switch.what, now or switch.current, switch.wanted))

    def onInstall(self, port=None) -> None:
        """Install, start, or fix what is missing: one button, and the state decides.

        `port` is only given by 'Try another port'.
        """
        deployment = self.deployment()
        backend = deployment.backend
        self._saveSettings()

        # Handled before the job starts, not inside it: a remedy needs a decision
        # (and for Docker a password prompt), and neither can be asked for from a
        # worker thread.
        if self._lastStatus is not None:
            current = self._serverState()
            if current.kind == serverState.State.NEEDS_SETUP:
                self._offerRemedy(current)
                return
        configure = self._configure()
        if configure and not self._confirmReplacingTheServer():
            return
        switches = self._allowBranchSwitches(deployment)
        if switches is None:
            return

        options = {"port": port if port else self.selectedPort(), "bind": self.selectedBind()}
        plan = backend.install_plan(self._lastStatus)
        fresh = not serverState.is_installed(self._lastStatus, backend)

        def task(progress_cb):
            # Each step is idempotent and the plan leaves out what is already
            # done, so a second press resumes after a failure or a cancel.
            result = None
            for step in plan:
                progress_cb(f"== {step.label} ==")
                result = step.run(deployment, progress_cb, options)
            return result

        def done(result):
            self._reportBranchSwitches(deployment, switches)
            if result.get("already_running") and not result.get("restarted"):
                self._log(_("Server already running, address and token registered."))
                slicer.util.showStatusMessage(
                    _("Server already running, address and token registered."), 5000)
            if configure and result.get("token"):
                url = backend.client_url(result)
                if url:
                    self._pointModulesHere(url, result["token"])
                else:
                    # Nothing is saved rather than an address the client would
                    # refuse: a Docker server needs a public address, a native
                    # one needs to report the pinned certificate it serves.
                    message = backend.no_url_message()
                    self._log(_(message))
                    slicer.util.warningDisplay(_(message))
            if result.get("healthy"):
                slicer.util.showStatusMessage(_("The tool server is running."), 5000)
            else:
                slicer.util.warningDisplay(plain_text(_(
                    "The server was started but {0}/health did not answer in time. It may "
                    "still be installing its dependencies: check the Log, or press Refresh in "
                    "a few minutes."
                ).format(result.get("url") or DEFAULT_SERVER_URL)))
            self.onRefresh()

        self._startJob(_("Installing and starting the server") if fresh else _("Starting the server"),
                       task, done, kind="install" if fresh else "start")

    def _offerRemedy(self, current) -> None:
        """What the first missing prerequisite asks of the user. Nothing here
        starts a job: the Docker installer needs a password, the others are
        instructions only this person can follow on this machine."""
        check = next((c for c in current.missing if c.remedy), None)
        if check is None:
            slicer.util.infoDisplay(plain_text(_("Something this machine needs is missing:\n\n{0}").format(
                "\n".join(f"  {c.label}: {c.detail}" for c in current.missing))))
            return
        key = check.remedy.key
        if key in ("docker_group", "docker_install"):
            docker = (self._lastStatus or {}).get("docker", {})
            reason = None
            if check.key == "compose":
                reason = _("Docker Compose is not installed.")
            self._offerDockerInstall(docker, reason)
        elif key == "git":
            slicer.util.infoDisplay(plain_text(_(backends.GIT_MISSING_MESSAGE)))
        elif key == "uv":
            slicer.util.infoDisplay(plain_text(self._uvMessage()))
        else:
            slicer.util.infoDisplay(plain_text(f"{check.label}: {check.detail}"))

    def confirmReplaceServer(self, text: str) -> bool:
        """Yes/no question before the modules are moved off a server that is
        already configured. Its own method so a test (or a different front end)
        can answer it without a dialog."""
        return bool(slicer.util.confirmYesNoDisplay(text, _("Use this server?")))

    def _confirmReplacingTheServer(self) -> bool:
        """Ask before replacing a remote server the modules already use. A lab
        server is the worst thing to overwrite without a word; nothing is asked
        when none is set, when the address is on this machine, or when it is
        already this server."""
        client = get_client()
        url = client.server_url
        if not serverState.configured_remote(url):
            return True
        if serverState.client_target(self._lastStatus, url) == serverState.CLIENT_HERE:
            return True
        question = _(
            "Every module currently uses the server at:\n\n  {0}\n\n"
            "Continue and use the server on this computer instead? The address and key of the "
            "current server are kept, and 'Switch back' in this panel restores them."
        ).format(url)
        if self.confirmReplaceServer(plain_text(question)):
            return True
        self._log(_("Not replacing the server in use: nothing was changed."))
        return False

    def _pointModulesHere(self, url: str, token: str) -> None:
        """Make this server the one every module uses, keeping the previous
        remote one so 'Switch back' can restore it."""
        client = get_client()
        previous = client.server_url
        if serverState.configured_remote(previous) and previous != url:
            settings = qt.QSettings()
            settings.setValue(_KEY_PREVIOUS_URL, previous)
            settings.setValue(_KEY_PREVIOUS_TOKEN, client.token)
            settings.sync()
        self._applyServerSettings(url, token)

    def onSwitchBack(self) -> None:
        settings = qt.QSettings()
        url = str(settings.value(_KEY_PREVIOUS_URL) or "")
        token = str(settings.value(_KEY_PREVIOUS_TOKEN) or "")
        if not url:
            return
        try:
            url = checked_server_url(url)
        except ValueError as exc:
            self._log(str(exc))
            slicer.util.errorDisplay(plain_text(str(exc)))
            return
        client = get_client()
        save_overrides(url, token, client.timeout)
        client.configure(server_url=url, token=token)
        settings.remove(_KEY_PREVIOUS_URL)
        settings.remove(_KEY_PREVIOUS_TOKEN)
        settings.sync()
        self._log(_("Every module uses {0} again.").format(url))
        slicer.util.showStatusMessage(_("Switched back to {0}.").format(url), 5000)
        self._applyActions()

    def onUseHere(self) -> None:
        """Point every module at the server that is already running here."""
        deployment = self.deployment()
        backend = deployment.backend
        status = self._lastStatus or {}
        if not self._confirmReplacingTheServer():
            return

        def task(progress_cb):
            return deployment.token(progress_cb=progress_cb)

        def done(token):
            url = backend.client_url(status)
            if url:
                self._pointModulesHere(url, token)
            else:
                message = backend.no_url_message()
                self._log(_(message))
                slicer.util.warningDisplay(_(message))
            self.onRefresh()

        self._startJob(_("Pointing the modules at this server"), task, done)

    def onTryAnotherPort(self) -> None:
        """The port is held by another program: start on the first free one. The
        server remembers it, so this has to be done once."""
        port = deploy.find_free_port()
        if port is None:
            slicer.util.errorDisplay(_("No free port was found between 8001 and 8099."))
            return
        self._log(_("Port {0} is free: starting there.").format(port))
        self.onInstall(port=port)

    @staticmethod
    def _uvMessage() -> str:
        return _(
            "uv is not installed. It builds the server's and the tools' Python environments, "
            "and nothing has been downloaded yet.\n\n"
            "Install it from https://docs.astral.sh/uv/ (in a terminal: "
            "curl -LsSf https://astral.sh/uv/install.sh | sh), then press the button again."
        )

    def _offerDockerInstall(self, docker: dict, reason=None) -> None:
        """Docker is missing or unusable. Offer to fix it, or say exactly how.

        The installer is a shell script from the cloned repository run through
        `pkexec` — a file the user has on disk and can read, rather than a
        curl-pipe-to-root started by a button.
        """
        deployment = self.deployment()
        reason = reason or docker.get("error") or _("Docker is not installed on this machine.")

        # Checked FIRST, because the remedy is not the one below. Docker is
        # installed and running here — the account simply is not in the
        # `docker` group — so re-running the installer would ask for a password,
        # take minutes, add the user to a group they are already in, and change
        # nothing. That is what this branch used to do.
        if docker.get("needs_group"):
            slicer.util.infoDisplay(_dockerGroupInstructions())
            return

        if not deployment.can_install_docker():
            slicer.util.errorDisplay(_(
                "{0}\n\nDocker has to be installed before the server can run.\n\n"
                "On Linux, run this in a terminal, then log out and back in:\n"
                "    sudo sh {1}\n\n"
                "On macOS or Windows, install Docker Desktop from "
                "https://docs.docker.com/get-docker/ and start it.\n\n"
                "Then come back here and press Install again."
            ).format(reason, deployment.install_docker_script))
            return

        if not slicer.util.confirmOkCancelDisplay(
            _("{0}\n\nInstall Docker now? It needs administrator rights, so you will be asked "
              "for your password. This can take a few minutes.").format(reason),
            _("Install Docker"),
        ):
            return

        def task(progress_cb):
            # The installer lives in the repository, so the clone comes first.
            deployment.clone(progress_cb=progress_cb)
            deployment.install_docker(progress_cb=progress_cb)
            return deployment.status(progress_cb=progress_cb)

        def done(status):
            if status.get("docker", {}).get("daemon"):
                slicer.util.infoDisplay(_(
                    "Docker is installed and usable. Press 'Install and start' to bring the "
                    "server up."
                ))
            else:
                # The expected ending of a SUCCESSFUL install, not a failure:
                # the script adds the user to the `docker` group, and a group
                # only exists in a new session. Same instructions as above, so
                # someone who meets this twice reads the same steps twice.
                slicer.util.infoDisplay(_dockerGroupInstructions(installed_just_now=True))
            self.onRefresh()

        self._startJob(_("Installing Docker"), task, done)

    def _offerGpuSetup(self) -> None:
        """A card is present but docker cannot reach it. Offer to fix it.

        The same shape as _offerDockerInstall — a script from the clone, run
        through `pkexec` — with one difference that has to be on screen BEFORE
        the password prompt: registering the runtime restarts the docker daemon,
        which stops every running container, this server included. Installing
        Docker happens on a machine where nothing is up yet; this does not.

        Never offered as a blocking step. The server runs perfectly on the CPU,
        so this is an improvement someone chooses, not a prerequisite.
        """
        deployment = self.deployment()

        if not deployment.can_install_docker():
            slicer.util.errorDisplay(_(
                "This machine has an NVIDIA card that Docker cannot reach, and the panel "
                "cannot set that up from here — it needs administrator rights through a "
                "graphical password prompt.\n\n"
                "Run this in a terminal instead:\n"
                "    sudo sh {0} --nvidia\n\n"
                "Then press Refresh. The server runs on the CPU in the meantime."
            ).format(deployment.install_docker_script))
            return

        if not slicer.util.confirmOkCancelDisplay(
            _("This machine has an NVIDIA card, but Docker cannot reach it — so the server "
              "would run on the CPU.\n\n"
              "Setting this up needs administrator rights, so you will be asked for your "
              "password. It also RESTARTS DOCKER, which stops every running container, "
              "including this server if it is up. Starting it again takes a few seconds.\n\n"
              "Set up GPU support now?"),
            _("Enable GPU support"),
        ):
            return

        def task(progress_cb):
            # The script lives in the repository, so the clone comes first.
            deployment.clone(progress_cb=progress_cb)
            # Stopped BEFORE the daemon restart, and that ordering is the whole
            # point. The compose services carry `restart: unless-stopped`, so a
            # container left running comes back up with docker — and once the
            # runtime is registered the panel drives the GPU service, a
            # DIFFERENT compose service publishing the same port. The resurrected
            # CPU container would hold 8000 against it and the next start would
            # fail the port preflight. A container stopped on purpose stays
            # stopped across a daemon restart, which is what makes this work.
            deployment.down(progress_cb=progress_cb)
            deployment.enable_gpu_runtime(progress_cb=progress_cb)
            return deployment.status(progress_cb=progress_cb)

        def done(status):
            if status.get("gpu", {}).get("nvidia_runtime"):
                slicer.util.infoDisplay(_(
                    "Docker can now reach the GPU. The server was stopped to do this — press "
                    "'Start server' to bring it back up, this time on the card."
                ))
            else:
                slicer.util.warningDisplay(_(
                    "The setup ran, but Docker still cannot reach the GPU. The Log has the "
                    "script's own output. The server keeps working on the CPU."
                ))
            self.onRefresh()

        self._startJob(_("Enabling GPU support"), task, done)

    def onUpdate(self) -> None:
        deployment = self.deployment()
        backend = deployment.backend
        # Passed on every update, for the same reason the branch is: `update`
        # recreates the container, so anything not restated here goes back to
        # its default, and the default is loopback. Without this, updating a
        # server published on a VPN address quietly took it off the network.
        bind = self.selectedBind()

        switches = self._allowBranchSwitches(deployment)
        if switches is None:
            return

        def task(progress_cb):
            backend.before_update(deployment, progress_cb)
            return deployment.update(progress_cb=progress_cb, bind=bind)

        def done(result):
            self._reportBranchSwitches(deployment, switches)
            summary = _(deploy.describe_update(result))
            self._log(summary)
            slicer.util.showStatusMessage(summary, 5000)
            if (result.get("restarted") or result.get("recreated")) and not result.get("healthy"):
                slicer.util.warningDisplay(_(
                    "The server was relaunched but did not answer in time. Check the Log."
                ))
            self.onRefresh()

        self._startJob(_("Updating the server"), task, done)

    def onStop(self) -> None:
        deployment = self.deployment()
        self._startJob(
            _("Stopping the server"),
            lambda progress_cb: deployment.down(progress_cb=progress_cb),
            lambda _result: self.onRefresh(),
        )

    def onShowLogs(self) -> None:
        deployment = self.deployment()
        self._startJob(
            _("Reading the server log"),
            lambda progress_cb: deployment.logs(200, progress_cb=progress_cb),
            lambda _result: None,
        )

    def onDownload(self) -> None:
        deployment = self.deployment()
        selected = sorted(self._selectedTools())
        if not selected:
            slicer.util.errorDisplay(_("Tick at least one tool before downloading."))
            return
        self._saveSettings()

        missing = self._missingBytes(selected)
        free = (self._catalog or {}).get("disk_free")
        question = _("Download data for: {0}?\n\nAbout {1} to transfer.").format(
            ", ".join(selected), _human(missing))
        if free is not None:
            question += _("\n{0} free on that disk.").format(_human(free))
            if missing and free < missing * 1.1:
                question += _("\n\nThat is cutting it fine — the download needs temporary space too.")
        if not slicer.util.confirmOkCancelDisplay(question, _("Download tool data")):
            return

        def task(progress_cb):
            return deployment.download_data(selected, progress_cb=progress_cb)

        def done(result):
            catalog = result.get("catalog")
            if catalog:
                self._renderCatalog(catalog)
            still = self._missingBytes(selected)
            if still:
                slicer.util.warningDisplay(_(
                    "Some items could not be downloaded ({0} still missing). The Log names "
                    "them; re-running fetches only those."
                ).format(_human(still)))
            else:
                slicer.util.showStatusMessage(_("Tool data is complete."), 5000)

        self._startJob(_("Downloading tool data"), task, done)

    def _applyServerSettings(self, url: str, token: str) -> None:
        """Make this deployment the one every other module talks to.

        Certificate verification is not among the things saved here, and
        cannot be: it is always on (tls.py), against the public authorities or,
        for https://127.0.0.1 only, the certificate pinned on this machine.
        `url` is the deployment's public https:// address, or a native server's
        pinned local one (deploy.client_url); an http:// URL is refused rather
        than saved, loopback included.
        """
        try:
            url = checked_server_url(url)
        except ValueError as exc:
            self._log(str(exc))
            slicer.util.errorDisplay(plain_text(str(exc)))
            return
        client = get_client()
        save_overrides(url, token, client.timeout)
        client.configure(server_url=url, token=token)
        self._log(_("Saved {0} as the server used by every module.").format(url))
        # A deployment bound to anything but loopback is reachable by other
        # machines, and what they will receive is unmodified. The bind warning
        # above covers who can READ the traffic; this covers what is in it.
        notify_uploads_are_not_deidentified(url)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _serverState(self):
        """The server's state, from the last status and what is running now."""
        job = self._jobKind if self._busyWithWork() else None
        return serverState.classify(
            self._lastStatus, self.backend, self._lastFacts, job, get_client().server_url)

    def _applyActions(self) -> None:
        """Show the buttons the state offers and hide every other one.

        Hidden, not greyed: a greyed 'Start server' beside a running server is
        the thing this replaced. The install button is one button whose text
        follows the state. Cancel belongs to _setBusy, which owns it.
        """
        if self._buildFailed or not hasattr(self, "useButton"):
            return
        backend = self.backend
        if self._lastStatus is None:
            # No answer yet: the install button is there and clickable (it must
            # never wait for a passive check), nothing else is offered.
            shown = {serverState.INSTALL, serverState.REFRESH}
            label = _(backend.texts.install_label)
        else:
            current = self._serverState()
            gpu_fixable = any(check.remedy and check.remedy.key == "gpu"
                              for check in backend.checks(self._lastStatus, self._lastFacts))
            settings = qt.QSettings()
            switch_back = bool(str(settings.value(_KEY_PREVIOUS_URL) or ""))
            shown = set(serverState.actions(current, gpu_fixable, switch_back).shown)
            label = _(serverState.install_label(current, backend))
        widgets = {
            serverState.INSTALL: self.installButton, serverState.UPDATE: self.updateButton,
            serverState.STOP: self.stopButton, serverState.USE_HERE: self.useButton,
            serverState.TRY_PORT: self.portButton, serverState.SWITCH_BACK: self.switchBackButton,
            serverState.LOGS: self.showLogsButton, serverState.REFRESH: self.refreshButton,
            serverState.CHECK_UPDATE: self.checkUpdateButton, serverState.GPU: self.gpuButton,
        }
        for key, widget in widgets.items():
            widget.setVisible(key in shown)
        self.installButton.setText(label)

    def _renderStatus(self, status: dict) -> None:
        tokens = design.tokens()
        backend = self.backend

        # The grid is what the backend says is worth checking on this machine:
        # Docker and Compose never appear on a Mac that does not use them.
        rows = {check.key: check for check in backend.checks(status, self._lastFacts)}
        for key, value in self._statusRows.items():
            name = self._statusNames[key]
            check = rows.get(key)
            name.setVisible(check is not None)
            value.setVisible(check is not None)
            if check is None:
                continue
            name.setText(_(check.label))
            value.setText(check.detail)
            value.setStyleSheet(f"color: {tokens['SUCCESS'] if check.ok else tokens['TEXT_MUTED']};")

        design.update_status_badge(
            self._statusBadge, bool(status.get("server", {}).get("healthy")))

        current = self._serverState()
        self.hintLabel.setText(_(backend.hint(current.kind, status, self._lastFacts)))
        self._applyBackendTexts()
        self._applyDeveloperMode()
        self._applyInstallFolder(current)
        self._applyActions()

    def _renderCatalog(self, catalog: dict) -> None:
        self._catalog = catalog
        previous = self._selectedTools() if self._toolRows else self._savedSelection()

        # Rebuilt wholesale rather than patched: the manifest can gain a tool
        # between two refreshes, and a row list that is only ever appended to
        # would keep showing one that was removed.
        for checkbox, label in self._toolRows.values():
            checkbox.setParent(None)
            label.setParent(None)
            checkbox.deleteLater()
            label.deleteLater()
        self._toolRows = {}
        while self._toolLayout.count():
            item = self._toolLayout.takeAt(0)
            sublayout = _qt_get(item, "layout")
            if sublayout:
                sublayout.deleteLater()

        for tool in catalog.get("tools", []):
            row = qt.QHBoxLayout()
            checkbox = qt.QCheckBox(tool["name"])
            checkbox.setChecked(tool["name"] in previous)
            checkbox.toggled.connect(self._updateSelectionSummary)
            label = design.hint_label(self._toolSummary(tool))
            row.addWidget(checkbox)
            row.addStretch(1)
            row.addWidget(label)
            self._toolLayout.addLayout(row)
            self._toolRows[tool["name"]] = (checkbox, label)

        self._updateSelectionSummary()

    def _toolSummary(self, tool: dict) -> str:
        if tool["complete"]:
            state = _("downloaded ({0})").format(_human(tool["size"]))
        elif tool["partial"]:
            state = _("partial — {0} of {1} left").format(
                _human(tool["missing_size"]), _human(tool["size"]))
        else:
            state = _("{0} to download").format(_human(tool["size"]))
        if self._registeredTools is not None and tool["name"] not in self._registeredTools:
            state += _("  ·  not exposed by this server")
        return state

    def _selectedTools(self):
        return {name for name, (checkbox, _label) in self._toolRows.items() if checkbox.checked}

    def _selectTools(self, predicate) -> None:
        for tool in (self._catalog or {}).get("tools", []):
            checkbox, _label = self._toolRows.get(tool["name"], (None, None))
            if checkbox is not None:
                checkbox.setChecked(bool(predicate(tool)))

    def _selectRegisteredTools(self) -> None:
        if self._registeredTools is None:
            slicer.util.warningDisplay(_(
                "The server is not answering, so its list of tools is unknown. Start it first."
            ))
            return
        self._selectTools(lambda tool: tool["name"] in self._registeredTools)

    def _missingBytes(self, names) -> int:
        wanted = set(names)
        return sum(tool["missing_size"] for tool in (self._catalog or {}).get("tools", [])
                   if tool["name"] in wanted)

    def _updateSelectionSummary(self, _checked=None) -> None:
        selected = self._selectedTools()
        if not selected:
            self.selectionLabel.setText(_("No tool selected."))
            self.downloadButton.setEnabled(False)
            return
        missing = self._missingBytes(selected)
        free = (self._catalog or {}).get("disk_free")
        text = _("{0} tool(s) selected — {1} to download").format(len(selected), _human(missing))
        if free is not None:
            text += _(", {0} free").format(_human(free))
        if not missing:
            text += _("  ·  everything selected is already on disk")
        self.selectionLabel.setText(text)
        self.downloadButton.setEnabled(True)
