import logging

import ctk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule, ScriptedLoadableModuleWidget

from ServerToolsCoreLib import config, design, get_client
from ServerToolsCoreLib.client import checked_server_url
from ServerToolsCoreLib.settings_qt import (
    clear_overrides,
    notify_uploads_are_not_deidentified,
    save_overrides,
)
from ServerToolsCoreLib.worker import BackgroundJob

logger = logging.getLogger("ServerToolsCore.settings")


class ServerToolsSettings(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Server Tools Settings")
        self.parent.categories = ["Automated Dental Tools.Advanced"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = ["Automated Dental Tools team"]
        self.parent.helpText = _(
            "Configure the remote tool server (URL, API key, TLS verification, timeout) used "
            "by every Automated Dental Tools module built on ServerToolsCore. Changes apply "
            "immediately and persist across Slicer restarts."
        )
        self.parent.acknowledgementText = ""


class ServerToolsSettingsWidget(ScriptedLoadableModuleWidget):
    """Thin settings panel over ServerToolsCoreLib.client.ToolServerClient.configure()."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.client = get_client()
        self.uiWidget = None
        self._statusBadge = None
        self._statusJob = None

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        self.uiWidget = qt.QWidget()
        self.layout.addWidget(self.uiWidget)
        rootLayout = qt.QVBoxLayout(self.uiWidget)

        self._statusBadge = design.status_badge()
        rootLayout.addWidget(self._statusBadge)

        box = ctk.ctkCollapsibleButton()
        box.text = _("Server connection")
        formLayout = qt.QFormLayout(box)
        rootLayout.addWidget(box)

        self.urlEdit = qt.QLineEdit()
        self.urlEdit.setToolTip(_("https:// only, e.g. https://tools.example.org"))
        formLayout.addRow(design.required_label(_("Server URL")), self.urlEdit)

        self.tokenEdit = qt.QLineEdit()
        self.tokenEdit.setEchoMode(qt.QLineEdit.Password)
        formLayout.addRow(design.required_label(_("API key")), self.tokenEdit)

        # Shown rather than omitted, so nobody hunts for the check box that was
        # here: certificate verification is not a setting any more. See
        # config.VERIFY_TLS for why, and for the one case that needs a path.
        formLayout.addRow(design.section_title(_("Verify TLS")),
                          design.hint_label(_("Always on - this client sends medical images.")))

        self.timeoutSpin = qt.QSpinBox()
        self.timeoutSpin.setRange(1, 3600)
        self.timeoutSpin.setSuffix(_(" s"))
        formLayout.addRow(design.section_title(_("Timeout")), self.timeoutSpin)

        self.saveButton = design.primary_button(_("Save"))
        self.restoreButton = design.danger_button(_("Restore defaults"))
        rootLayout.addWidget(self.saveButton)
        rootLayout.addWidget(self.restoreButton)
        rootLayout.addStretch(1)

        self.saveButton.clicked.connect(self.onSaveButton)
        self.restoreButton.clicked.connect(self.onRestoreButton)

        design.apply(self.uiWidget)
        self._loadFromClient()

    def cleanup(self) -> None:
        if self._statusJob:
            self._statusJob.cancel()
            self._statusJob = None

    def enter(self) -> None:
        design.apply(self.uiWidget)
        self._refreshStatus()

    def _loadFromClient(self) -> None:
        """Reflect whatever is actually active right now: a saved override if
        one was applied at startup, otherwise config.py's defaults."""
        self.urlEdit.text = self.client.server_url
        self.tokenEdit.text = self.client.token
        self.timeoutSpin.value = self.client.timeout
        self._refreshStatus()

    def onSaveButton(self) -> None:
        server_url = self.urlEdit.text.strip()
        if not server_url:
            slicer.util.errorDisplay(_("Server URL cannot be empty."))
            return

        # Checked before anything is written, so a refused URL never reaches
        # the settings file: saving first would leave a value on disk that
        # `load_overrides` drops again at the next start, with nothing on
        # screen to say why the server reverted. A loopback http:// comes
        # back as https://, and the field shows what was actually saved.
        try:
            server_url = checked_server_url(server_url)
        except ValueError as exc:
            slicer.util.errorDisplay(str(exc))
            return
        self.urlEdit.text = server_url

        token = self.tokenEdit.text
        timeout = self.timeoutSpin.value

        save_overrides(server_url, token, timeout)
        self.client.configure(server_url=server_url, token=token, timeout=timeout)
        logger.info("Server settings saved: url=%s timeout=%s", server_url, timeout)
        slicer.util.showStatusMessage(_("Server settings saved."), 3000)
        # After the save, not before: the notice is about what this server will
        # receive, and a user who cancels out of it has still changed nothing
        # they did not ask to change.
        notify_uploads_are_not_deidentified(server_url)
        self._refreshStatus()

    def onRestoreButton(self) -> None:
        clear_overrides()
        self.client.configure(
            server_url=config.SERVER_URL,
            token=config.API_TOKEN,
            verify_tls=config.VERIFY_TLS,
            timeout=config.TIMEOUT,
        )
        self._loadFromClient()
        slicer.util.showStatusMessage(_("Restored default server settings."), 3000)

    def _refreshStatus(self) -> None:
        """Owned by the widget, not a local - see ServerToolWidgetBase._refreshServerStatus()."""
        if self._statusJob:
            self._statusJob.cancel()

        def task(_progress_cb):
            return self.client.health()

        self._statusJob = BackgroundJob(
            task, on_success=self._onStatusChecked, on_error=lambda _exc: self._onStatusChecked(False)
        )
        self._statusJob.start()

    def _onStatusChecked(self, ok: bool) -> None:
        self._statusJob = None
        if self._statusBadge:
            design.update_status_badge(self._statusBadge, ok)
