"""
Crop both temporomandibular joints (TMJ) of a CBCT.

Thin GUI over the remote `TMJ_Crop` tool. The server orients the scan to the
Frankfurt horizontal (ASO), segments the mandible (AMASSS), then cuts one box
around each condyle; the left crop is mirrored so both sides share one
orientation. Nothing is installed into Slicer's interpreter.

The panel is generated from the server's `GET /tools` entry.
"""

from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule

from ServerToolsCoreLib.base_widget import ServerToolWidgetBase


class TMJ_CropRemote(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("TMJ Crop Remote")
        self.parent.categories = ["Automated Dental Tools Remote"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = [
            "Jules Grivot Pélisson (UNC)",
        ]
        self.parent.helpText = _("""
        Crops the right and the left temporomandibular joint (TMJ) of CBCT scans, computed remotely by
        the Automated Dental Tools server. Give it one scan or a whole folder: each case comes
        back as two crops of the same size and orientation (the left one mirrored), a report
        with the position of each condyle, and a quick-look image to check the box.
        See more information in <a href="https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalTools">documentation</a>.
        """)
        self.parent.acknowledgementText = _("This module was supported by NIDCR R01 024450.")


class TMJ_CropRemoteWidget(ServerToolWidgetBase):
    """Thin GUI: everything else (HTTP, async, form generation, styling, lifecycle)
    lives in ServerToolsCoreLib. See ARCHITECTURE.md.

    The schema already says what the panel needs: `scans` takes a file or a
    folder, `scale_by` is a dropdown, `scale` a number, and `output_kind: "files"`
    means each case's results come back as one archive unpacked into the output
    folder the user picks. What a server cannot know is which of those files
    belong in the 3D scene, hence `_LOADABLE`.
    """

    TOOL_NAME = "TMJ_Crop"
    AUTO_UI = True

    # A cohort writes two crops per scan: loading them all would bury the scene.
    MAX_RESULTS_TO_LOAD = 12

    # The crops are intensity volumes (a box cut from the CBCT), not label
    # maps. The per-scan `.json` report and the `_qc.png` quick view are for the
    # output folder, not the scene, so they are deliberately absent.
    #
    # The two optional extras are the oriented scan (a volume) and the mandible
    # mask (labelled voxels, hence a labelmap, never a segmentation node).
    _LOADABLE = (
        ("*_R.nii.gz", "volume"),
        ("*_L.nii.gz", "volume"),
        ("*_oriented.nii.gz", "volume"),
        ("*_mandible.nii.gz", "labelmap"),
    )

    def handleResult(self, result) -> None:
        """Unpack the archive (base class), then optionally load what it held."""
        super().handleResult(result)

        self._maybeLoadResults()
