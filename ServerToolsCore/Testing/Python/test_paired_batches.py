"""Splitting a cohort for a tool whose inputs are PAIRED.

The tool says which files go together (asked through the server's
`POST /tools/{tool}/pairs`, faked here); the panel packs patients into batches
and every batch carries the SAME patients in every paired folder. What must
hold: no patient is unpaired by the split, no batch holds a lone patient when
there are several, a patient with no partner is reported and never sent, and
anything the server cannot answer falls back to sending the cohort whole.
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import test_hosted_test_files as fixtures  # noqa: F401,E402 - installs the stubs

import slicer  # noqa: E402
from ServerToolsCoreLib import base_widget  # noqa: E402
from ServerToolsCoreLib.errors import ServerToolError  # noqa: E402


class _Widget:
    def __init__(self, path):
        self.currentPath = path

    def is_folder(self):
        return os.path.isdir(self.currentPath)


class _Client:
    """Pairs by the part of a name before its first underscore."""

    def __init__(self, fail=False):
        self.fail = fail
        self.asked = []

    def pairs(self, tool, inputs, arguments=None):
        self.asked.append((tool, inputs, arguments))
        if self.fail:
            raise ServerToolError("no pairs today")
        key = lambda name: name.split("/")[-1].split("_")[0]
        t1 = {key(n): n for n in inputs["t1"]}
        t2 = {key(n): n for n in inputs["t2"]}
        both = sorted(set(t1) & set(t2))
        return {"groups": [{"key": k, "keys": [k], "entries": {"t1": [t1[k]], "t2": [t2[k]]}} for k in both],
                "shared": {},
                "unpaired": {"t1": sorted(set(t1) - set(both)), "t2": sorted(set(t2) - set(both))}}

    def batch_policy(self):
        return None


class PairedBatchesTest(unittest.TestCase):
    PLAN = {"axes": ["t1", "t2"], "max_mb": 0, "max_files": 2}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="paired_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.t1 = os.path.join(self.root, "T1")
        self.t2 = os.path.join(self.root, "T2")
        self.output = os.path.join(self.root, "out")
        for folder in (self.t1, self.t2, self.output):
            os.makedirs(folder)
        self.warnings = []
        slicer.util.warningDisplay = lambda text, **kw: self.warnings.append(text)
        panel = base_widget.ServerToolWidgetBase.__new__(base_widget.ServerToolWidgetBase)
        panel.TOOL_NAME = "AREG"
        panel._runs = []
        panel._runsStarted = 0
        panel._schema = {"arguments": {"t1": {"type": "path"}, "t2": {"type": "path"}},
                         "paired_batch": dict(self.PLAN)}
        panel._inputModes = {"t1": "folder_zip", "t2": "folder_zip"}
        panel._inputWidgets = {"t1": _Widget(self.t1), "t2": _Widget(self.t2)}
        panel._hiddenArgs = set()
        panel._outputFolderWidget = _Widget(self.output)
        panel.collectArgs = lambda: {}
        panel._pumpRuns = lambda: None
        panel.client = _Client()
        self.panel = panel
        self.addCleanup(lambda: [run.workspace.__exit__(None, None, None) for run in panel._runs])

    def _patients(self, *keys, t2_only=()):
        def write(path, size):
            with open(path, "wb") as handle:
                handle.write(b"x" * size)
        for key in keys:
            write(os.path.join(self.t1, f"{key}_T1.nii.gz"), 10)
            write(os.path.join(self.t2, f"{key}_T2.nii.gz"), 10)
        for key in t2_only:
            write(os.path.join(self.t2, f"{key}_T2.nii.gz"), 1)

    def _packed(self, run, axis):
        with zipfile.ZipFile(run.files[axis]) as archive:
            return sorted(i.filename.split("_")[0] for i in archive.infolist() if not i.is_dir())

    def test_every_batch_carries_the_same_patients_in_both_folders(self):
        self._patients("P1", "P2", "P3", "P4")
        self.panel.onApplyButton()
        self.assertEqual(len(self.panel._runs), 2)
        for run in self.panel._runs:
            self.assertEqual(self._packed(run, "t1"), self._packed(run, "t2"))
        self.assertEqual([self._packed(r, "t1") for r in self.panel._runs], [["P1", "P2"], ["P3", "P4"]])

    def test_a_lone_patient_is_folded_into_its_neighbour(self):
        self._patients("P1", "P2", "P3", "P4", "P5")
        self.panel.onApplyButton()
        self.assertEqual([self._packed(r, "t1") for r in self.panel._runs], [["P1", "P2"], ["P3", "P4", "P5"]])

    def test_a_patient_without_partner_is_reported_and_not_sent(self):
        self._patients("P1", "P2", "P3", "P4", t2_only=("P9",))
        self.panel.onApplyButton()
        self.assertTrue(any("P9" in text for text in self.warnings))
        sent = [p for r in self.panel._runs for p in self._packed(r, "t2")]
        self.assertNotIn("P9", sent)

    def test_the_cohort_counts_patients(self):
        self._patients("P1", "P2", "P3", "P4")
        self.panel.onApplyButton()
        self.assertEqual(self.panel._runs[0].cohort.total_scans, 4)

    def test_a_server_that_cannot_pair_gets_the_cohort_whole(self):
        self._patients("P1", "P2", "P3", "P4")
        self.panel.client = _Client(fail=True)
        self.panel.onApplyButton()
        self.assertEqual(len(self.panel._runs), 1)
        self.assertEqual(self._packed(self.panel._runs[0], "t1"), ["P1", "P2", "P3", "P4"])

    def test_a_facade_splits_only_for_a_mode_that_pairs(self):
        self._patients("P1", "P2", "P3", "P4")
        self.panel._schema["paired_batch"] = {"by": "mode", "modes": {"CBCT": dict(self.PLAN)}}
        self.panel.collectArgs = lambda: {"mode": "IOS to CBCT"}
        self.panel.onApplyButton()
        self.assertEqual(len(self.panel._runs), 1)
        self.panel._runs.clear()
        self.panel.collectArgs = lambda: {"mode": "CBCT"}
        self.panel.onApplyButton()
        self.assertEqual(len(self.panel._runs), 2)
        self.assertEqual(self.panel.client.asked[-1][2], {"mode": "CBCT"})


if __name__ == "__main__":
    unittest.main()
