"""
Per-tooth labelling of intraoral surface scans (Crown_Seg).

Thin GUI over the remote `Crown_Seg` tool. The segmentation runs on the server,
so `shapeaxi` and its torch stack are never installed into Slicer's interpreter
and no checkpoint is downloaded to this machine -- which is the whole reason the
old path shelled out to a `dentalmodelseg` binary in Slicer's own bin directory.

Its own module rather than a step inside ALI: the labelled array it writes is the
precondition for ALI's IOS landmarks and for the IOS modes of ASO, AREG and
FlexReg, so four chains need it and none of them owns it.
"""

from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule

from ServerToolsCoreLib.base_widget import ServerToolWidgetBase


class Crown_Seg(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Crown Segmentation")
        self.parent.categories = ["Automated Dental Tools Remote"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = [
            "Juan Carlos Prieto (UoNC)",
            "Lucia Cevidanes (UoM)",
        ]
        self.parent.helpText = _("""
        Labels every tooth of an intraoral surface scan with its dental number, written as a
        point-data array on a copy of the mesh, computed remotely by the Automated Dental Tools
        server. That array is what ALI's IOS landmark identification and the IOS modes of ASO,
        AREG and FlexReg read.
        See more information in <a href="https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalTools">documentation</a>.
        """)
        self.parent.acknowledgementText = ""


class Crown_SegWidget(ServerToolWidgetBase):
    """Thin GUI: everything else (HTTP, async, form generation, styling, lifecycle)
    lives in ServerToolsCoreLib. See ARCHITECTURE.md.

    Nothing about the tool is restated here. `meshes` is a packaged tool's `path`,
    which the client already gives a picker taking a file OR a folder, and the
    numbering scheme, the array name and the rest are schema arguments rendered by
    formgen -- so a new numbering appears in this panel with no client release.
    """

    TOOL_NAME = "Crown_Seg"
    LOAD_RESULTS_LABEL = _("Load the labelled meshes into the scene when done")

    # A cohort of intraoral scans legitimately returns dozens of meshes; twelve is
    # what the other file-producing panels use.
    MAX_RESULTS_TO_LOAD = 12

    # A labelled surface is a MODEL, not a segmentation: the labels ride as a
    # point-data array on the mesh the caller already had, and loading it as a
    # segmentation node would relabel geometry that is already labelled.
    _LOADABLE = (
        ("*.vtk", "model"),
        ("*.vtp", "model"),
        ("*.stl", "model"),
        ("*.obj", "model"),
    )

    def handleResult(self, result) -> None:
        """Unpack the archive (base class), then optionally load what it held."""
        super().handleResult(result)

        self._maybeLoadResults()
