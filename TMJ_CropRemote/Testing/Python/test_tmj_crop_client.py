"""What the TMJ Crop panel declares. The panel itself is generated from `GET /tools`."""

import fnmatch
import os
import sys
import unittest

_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_CORE = os.path.join(_REPO, "ServerToolsCore")
sys.path.insert(0, os.path.join(_CORE, "Testing", "Python"))
sys.path.insert(0, _CORE)
sys.path.insert(0, os.path.join(_REPO, "TMJ_CropRemote"))

import test_hosted_test_files as fixtures  # noqa: F401,E402 - installs the stubs

from ServerToolsCoreLib import slicer_io  # noqa: E402

import TMJ_CropRemote  # noqa: E402

Widget = TMJ_CropRemote.TMJ_CropRemoteWidget


class DeclarationsTest(unittest.TestCase):
    def test_it_drives_the_tool_of_that_name(self):
        self.assertEqual(Widget.TOOL_NAME, "TMJ_Crop")

    def test_every_loadable_kind_has_a_loader(self):
        """A kind nobody registered fails at the END of a run, after the work is done."""
        for _pattern, kind in Widget._LOADABLE:
            self.assertIn(kind, slicer_io._LOADERS, kind)

    def test_crops_and_oriented_scan_are_volumes_the_mandible_is_a_labelmap(self):
        self.assertEqual(
            sorted(Widget._LOADABLE),
            [
                ("*_L.nii.gz", "volume"),
                ("*_R.nii.gz", "volume"),
                ("*_mandible.nii.gz", "labelmap"),
                ("*_oriented.nii.gz", "volume"),
            ],
        )

    def test_each_file_the_tool_writes_matches_exactly_one_pattern(self):
        """Two matches would open one file twice; none would drop an output."""
        for name, count in (
            ("case_R.nii.gz", 1), ("case_L.nii.gz", 1),
            ("case_oriented.nii.gz", 1), ("case_mandible.nii.gz", 1),
        ):
            matches = [p for p, _k in Widget._LOADABLE if fnmatch.fnmatch(name, p)]
            self.assertEqual(len(matches), count, name)

    def test_the_quick_view_and_the_report_stay_out_of_the_scene(self):
        for name in ("case_qc.png", "case.json"):
            self.assertFalse(
                any(fnmatch.fnmatch(name, pattern) for pattern, _k in Widget._LOADABLE), name)

    def test_a_cohort_cannot_flood_the_scene(self):
        self.assertLessEqual(Widget.MAX_RESULTS_TO_LOAD, 12)


if __name__ == "__main__":
    unittest.main()
