"""What the AutoCrop3D panel declares, and the one thing it adds to the shared one.

The panel itself is generated from `GET /tools`; what this module owns is four
declarations and a single line of feedback. Each is tested for the reason it
exists, not for being present.
"""

import os
import sys
import unittest

_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_CORE = os.path.join(_REPO, "ServerToolsCore")
sys.path.insert(0, os.path.join(_CORE, "Testing", "Python"))
sys.path.insert(0, _CORE)
sys.path.insert(0, os.path.join(_REPO, "AutoCrop3DRemote"))

import test_hosted_test_files as fixtures  # noqa: F401,E402 - installs the stubs

from ServerToolsCoreLib import formgen, slicer_io  # noqa: E402

import AutoCrop3DRemote  # noqa: E402

Widget = AutoCrop3DRemote.AutoCrop3DRemoteWidget


class DeclarationsTest(unittest.TestCase):
    def test_it_drives_the_tool_of_that_name(self):
        self.assertEqual(Widget.TOOL_NAME, "AutoCrop3D")

    def test_every_loadable_kind_has_a_loader(self):
        """A kind nobody registered fails at the END of a run, after the work is
        done -- which is exactly how ASO shipped with unreadable landmarks."""
        for _pattern, kind in Widget._LOADABLE:
            self.assertIn(kind, slicer_io._LOADERS, kind)

    def test_no_two_patterns_can_match_one_file(self):
        """A file matching two patterns is opened twice: the same scan in the
        scene as a volume and again as something else."""
        import fnmatch

        patterns = [pattern for pattern, _kind in Widget._LOADABLE]
        for index, pattern in enumerate(patterns):
            for other in patterns[index + 1:]:
                self.assertFalse(
                    fnmatch.fnmatch(pattern.replace("*", "x"), other)
                    or fnmatch.fnmatch(other.replace("*", "x"), pattern),
                    f"{pattern} and {other} can match one file")

    def test_a_crop_is_a_volume_not_a_labelmap(self):
        """It keeps the scan's intensities; only the extent changes. Loaded as a
        labelmap it would arrive coloured by a table that means nothing."""
        kinds = {kind for pattern, kind in Widget._LOADABLE if "vtk" not in pattern}
        self.assertEqual(kinds, {"volume"})

    def test_the_surface_it_can_write_loads_as_a_model(self):
        self.assertIn(("*.vtk", "model"), Widget._LOADABLE)

    def test_a_cbct_is_rendered_with_the_dental_preset(self):
        self.assertEqual(Widget.VOLUME_RENDERING, "CT-AAA")

    def test_the_report_it_names_is_the_one_the_tool_writes(self):
        self.assertEqual(Widget.RUN_REPORT, "AutoCrop3D_report.json")


class TheBoxComesFromTheSceneTest(unittest.TestCase):
    """The reason this module declares SCENE_INPUTS at all."""

    def test_the_roi_row_is_offered_boxes(self):
        self.assertEqual(Widget.SCENE_INPUTS, {"roi": ("roi",)})

    def test_that_kind_exists_in_the_core(self):
        """Declaring a kind formgen does not know would make the row offer
        nothing, silently -- the failure this whole seam keeps producing."""
        for kinds in Widget.SCENE_INPUTS.values():
            for kind in kinds:
                self.assertIn(kind, formgen.SCENE_NODE_KINDS, kind)

    def test_the_scan_row_needs_no_declaration(self):
        """`scan` is in formgen's name vocabulary, so the generic rule answers
        it. Restating it here would be a second place to keep in sync."""
        self.assertNotIn("scans", Widget.SCENE_INPUTS)
        self.assertEqual(
            formgen.scene_kinds_for({"type": "path", "types": ["path"]}, "scans"),
            ("volume",))


class WhatTheRunSaysTest(unittest.TestCase):
    """A crop looks like nothing: the result opens exactly where the scan was.

    A patient the run SKIPPED leaves no trace on screen at all, and that is
    upstream's defect in its purest form -- a cohort whose identifiers held an
    underscore matched no ROI, was skipped in full, and exited 0.
    """

    def _panel(self, report):
        panel = Widget.__new__(Widget)
        panel._producedRoot = "/out"
        panel.said = []
        panel._showPhase = panel.said.append
        panel._readRunReport = lambda _root: report
        return panel

    def test_it_counts_what_was_cropped_against_what_was_found(self):
        panel = self._panel({"summary": {"scans_found": 4, "cropped": 4}})

        panel._sayWhatWasCropped()
        self.assertEqual(panel.said, ["4 of 4 scan(s) cropped."])

    def test_a_patient_with_no_box_is_named(self):
        """Named, not counted: which patient found no ROI is the one thing a
        clinician can act on."""
        panel = self._panel({
            "summary": {"scans_found": 3, "cropped": 1},
            "without_a_roi": [{"scan": "a.nii.gz", "patient": "Patient_01"},
                              {"scan": "b.nii.gz", "patient": "Patient_02"}],
        })

        panel._sayWhatWasCropped()
        self.assertIn("1 of 3", panel.said[0])
        self.assertIn("Patient_01", panel.said[0])
        self.assertIn("Patient_02", panel.said[0])

    def test_a_long_list_is_cut_rather_than_filling_the_panel(self):
        panel = self._panel({
            "summary": {"scans_found": 40, "cropped": 0},
            "without_a_roi": [{"patient": f"P{n:02d}"} for n in range(40)],
        })

        panel._sayWhatWasCropped()
        self.assertEqual(panel.said[0].count(","), 4, panel.said[0])

    def test_no_report_says_nothing_rather_than_guessing(self):
        """Best effort: the results are on disk and in the scene either way."""
        panel = self._panel(None)

        panel._sayWhatWasCropped()
        self.assertEqual(panel.said, [])

    def test_a_report_of_another_shape_says_nothing_either(self):
        """The server may publish a report this panel does not recognise; a
        finished run must not end on a KeyError."""
        panel = self._panel({"summary": {}})

        panel._sayWhatWasCropped()
        self.assertEqual(panel.said, [])


if __name__ == "__main__":
    unittest.main()
