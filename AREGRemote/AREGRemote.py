"""
Automated REGistration (AREG) of two timepoints of the same patient, for
cone-beam computed tomography (CBCT) and intraoral surface scans (IOS).

Thin GUI over the remote `AREG` tool. The registration runs on the server, so
nothing is installed into Slicer's interpreter, no conda environment is created,
and neither the segmentation models nor the registration checkpoint are ever
downloaded to this machine.

Replaces the former local module, which drove the AREG_CBCT / AREG_IOS /
AREG_IOSCBCT CLIs from inside Slicer and chained them with AMASSS, ASO, ALI and
CrownSeg by hand. `AREG_Method/` is left in the tree but is no longer wired to
this one.

**There is no `.ui` file here, and that is the design rather than an omission.**
The panel is built from the server's schema by `ServerToolsCoreLib.formgen`, so a
field added to the tool server-side appears here with no client release. The
former module's `Resources/UI/AREG.ui` was kept for a while and then deleted,
because keeping it invited edits that changed nothing: CMakeLists did not ship
it and no code read it, so a UI change made there was a silent no-op -- and one
cost a day of looking for it in the wrong place.

Authors:
- Nathan Hutin (UoM)
- Luc Anchling (UoM)
"""

import glob
import logging
import os

import qt
import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule

from ServerToolsCoreLib import slicer_io
from ServerToolsCoreLib.base_widget import ServerToolWidgetBase

logger = logging.getLogger("AREGRemote")


class AREGRemote(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("AREG Remote")
        self.parent.categories = ["Automated Dental Tools Remote"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = [
            "Nathan Hutin (UoM)",
            "Luc Anchling (UoM)",
            "Lucia Cevidanes (UoM)",
            "Juan Carlos Prieto (UoNC)",
        ]
        self.parent.helpText = _("""
        Registers a follow-up scan onto a baseline scan of the same patient, remotely, on the
        Automated Dental Tools server. Give it a T1 folder and a T2 folder; each pair comes back
        with the T2 moved into the T1's frame and the transform that moved it, so growth or
        treatment change can be measured.
        CBCT registration is confined to the anatomy you pick (cranial base, mandible, maxilla);
        intraoral registration uses the palate, which orthodontic treatment does not move.
        See more information in <a href="https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalTools">documentation</a>.
        """)
        self.parent.acknowledgementText = _("""
        This file was developed by Nathan Hutin (UoM) and Luc Anchling (UoM) and was supported by
        NIDCR R01 024450, AA0F Dewel Memorial Biomedical Research award and by Research
        Enhancement Award Activity 141 from the University of the Pacific,
        Arthur A. Dugoni School of Dentistry.
        """)


class AREGRemoteWidget(ServerToolWidgetBase):
    """Thin GUI: everything else (HTTP, async, form generation, styling, lifecycle)
    lives in ServerToolsCoreLib. See ARCHITECTURE.md.

    Nothing about the pipeline lives here. Which anatomical regions can be
    registered on, which modes exist, what each mode additionally needs, and the
    fact that a CBCT run is never asked for an intraoral checkpoint, are all in
    the server's schema and rendered by formgen — so a region added server-side
    appears in this panel with no client release.

    The old local module spent 2574 lines on the same panel, plus three
    `AREG_Method` classes whose job was to build the argument dictionaries for
    five different CLIs and run them in sequence. That sequencing is the
    server's now: AREG calls AMASSS, ASO and CrownSeg in-process, so this module
    makes ONE request where the old one launched up to six processes and polled
    a log file for progress.

    In particular the mode is *asked for*, never guessed: a `.zip` can hold CBCT
    volumes or intraoral meshes, so no extension could tell this module which
    pipeline the user wanted. The same goes for `ios_patch`, which picks the
    region the two timepoints are aligned on and, with it, the arch: the palate
    (predicted, upper) or the mucogingival line (built from landmarks, lower).
    The panel shows the checkpoint for one and the landmark folder for the
    other, and that switch is `visible_when` in the schema, not code here.
    """

    TOOL_NAME = "AREG"
    # FILE_INPUTS names `patients` only (below): the schema types `t1`, `t2`
    # and `t1_masks` as ["folder", "zip_file"], so "auto" already gives each
    # one a picker that takes a folder and zips it before upload. `t1`/`t2`
    # are also flagged server_selectable, which puts the hosted test cohorts
    # above that picker. Nothing else here names a type, an extension, or a
    # mode.
    #
    # No RESULT_KIND either: output_kind "files" is one registered scan and its
    # transform per case and per region, plus AREG_report.json, bundled into one
    # .zip and unpacked into the output folder the user picks.
    AUTO_UI = True
    # The one input the schema cannot describe precisely: `patients` is a
    # FOLDER of patient folders, never one file, and a packaged tool's path
    # takes either. Forced to the folder picker so the panel does not offer
    # a choice that can only fail.
    FILE_INPUTS = {"patients": "folder_zip"}

    # Loading is a courtesy for the single-pair run: a cohort registered on
    # three regions legitimately returns hundreds of files, and loading them all
    # would be worse than useless.
    MAX_RESULTS_TO_LOAD = 12
    # The CBCT every IOS was registered onto is the scene's frame of reference;
    # without a rendering the 3D view showed the arches floating on nothing.
    # The base class renders a scan only when the run returned exactly one, so
    # a CBCT run handing back both timepoints keeps an empty 3D view.
    VOLUME_RENDERING = "CT-AAA"
    # One colour per landmark set and one per mesh, in the order the files sort.
    # Every markups file loaded in Slicer's default colour, so the orientation
    # landmarks, the CBCT's and each arch's moved copy were one indistinct cloud,
    # and both arches the same grey. Kept apart so a landmark never takes the
    # colour of a mesh it sits on.
    _MARKUPS_COLORS = (
        (0.90, 0.20, 0.20),  # red
        (0.20, 0.75, 0.25),  # green
        (0.20, 0.45, 1.00),  # blue
        (1.00, 0.80, 0.10),  # yellow
        (0.80, 0.30, 0.90),  # purple
        (0.10, 0.80, 0.85),  # cyan
    )
    _MODEL_COLORS = (
        (0.95, 0.85, 0.65),  # ivory
        (0.65, 0.80, 1.00),  # light blue
        (1.00, 0.70, 0.75),  # pink
        (0.70, 0.95, 0.70),  # light green
    )
    # Pattern -> how to load it. A registered CBCT is a VOLUME, not a
    # segmentation: AREG moves a scan, it does not label one. `*.tfm` is
    # deliberately absent — the transform is there to carry a measurement back
    # onto the original acquisition, and loading it into the scene applies
    # nothing by itself.
    _LOADABLE = (
        ("*.nii.gz", "volume"),
        ("*.nii", "volume"),
        ("*.nrrd.gz", "volume"),
        ("*.nrrd", "volume"),
        ("*.gipl.gz", "volume"),
        ("*.gipl", "volume"),
        ("*.vtk", "model"),
        ("*.vtp", "model"),
        # The landmarks a run hands back when "Registration landmarks" is
        # ticked (each mesh's, moved with it, and the CBCT's it was registered
        # on) or "Orientation landmarks" (the ones ASO oriented the CBCT on).
        # Without these the scene showed the meshes on the CBCT and no point
        # to judge the fit by. `*_lm.json` is the moved copy of a plain-JSON
        # input; AREG_report.json matches neither pattern.
        ("*.mrk.json", "markups"),
        ("*_lm.json", "markups"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loadResultsCheckBox = None

    def addExtraWidgets(self, layout) -> None:
        self._loadResultsCheckBox = qt.QCheckBox(_("Load the results into the scene when done"))
        self._loadResultsCheckBox.setChecked(True)
        layout.addWidget(self._loadResultsCheckBox)

    def handleResult(self, result) -> None:
        """Unpack the archive (base class), then optionally load what it held."""
        super().handleResult(result)

        if not (self._loadResultsCheckBox and self._loadResultsCheckBox.isChecked()):
            return
        self._loadResults()

    def _showLoadedResults(self, opened: list) -> None:
        """Show the results (base class), each landmark set and mesh in its own colour."""
        super()._showLoadedResults(opened)
        self._colorResults(opened)

    @classmethod
    def _colorResults(cls, opened: list) -> None:
        """Give every markups node and every model a colour of its own.

        Ordered by node name, which is the file's, so a lower arch and its
        landmarks get the same colours on every run. Best effort: a colour is
        never worth failing a run that finished.
        """
        for kind, palette in (("markups", cls._MARKUPS_COLORS), ("model", cls._MODEL_COLORS)):
            nodes = sorted((node for k, node in opened if k == kind and node is not None),
                           key=lambda node: node.GetName())
            for index, node in enumerate(nodes):
                color = palette[index % len(palette)]
                try:
                    display = node.GetDisplayNode()
                    if display is None:
                        continue
                    display.SetColor(*color)
                    if kind == "markups":
                        # A markups point is drawn in its SELECTED colour, which
                        # every point is until clicked.
                        display.SetSelectedColor(*color)
                except Exception as exc:  # noqa: BLE001 - never worth failing a run
                    logger.warning("Could not colour %s: %s", node.GetName(), exc)

    @classmethod
    def _findResults(cls, outputDir: str) -> list:
        """[(path, kind)] for every result with a loader, in the per-region,
        per-case tree the server built."""
        return sorted(
            (path, kind)
            for pattern, kind in cls._LOADABLE
            for path in glob.glob(os.path.join(outputDir, "**", pattern), recursive=True)
        )
