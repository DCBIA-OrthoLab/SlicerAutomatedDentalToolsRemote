"""The install screen: a list of steps and one global, weighted progress bar.

    python3 -m unittest test_progress

Pure: the panel feeds it the lines a step prints, it answers which step runs,
which are done and how far along the whole install is. Nothing is estimated in
minutes; the bar moves when a step finishes, and the line under the running
step says what it is doing.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))
sys.path.insert(0, _HERE)

import fixtures  # noqa: E402
from SlicerCloudLib import backends, progress  # noqa: E402


def plan():
    return backends.NATIVE.install_plan(fixtures.native_uninstalled())


class TestSteps(unittest.TestCase):
    def test_nothing_has_started(self):
        run = progress.InstallProgress(plan())
        self.assertEqual(run.fraction(), 0.0)
        self.assertEqual({row.mark for row in run.rows()}, {"waiting"})

    def test_a_step_banner_starts_that_step_and_finishes_the_one_before(self):
        steps = plan()
        run = progress.InstallProgress(steps)
        run.feed(f"== {steps[0].label} ==")
        self.assertEqual(run.rows()[0].mark, "running")
        run.feed(f"== {steps[1].label} ==")
        self.assertEqual([row.mark for row in run.rows()[:2]], ["done", "running"])
        self.assertEqual(run.rows()[2].mark, "waiting")

    def test_the_bar_is_weighted_by_the_steps(self):
        steps = plan()
        total = sum(step.weight for step in steps)
        run = progress.InstallProgress(steps)
        run.feed(f"== {steps[0].label} ==")
        run.feed(f"== {steps[1].label} ==")
        self.assertAlmostEqual(run.fraction(), steps[0].weight / total)

    def test_the_long_step_is_most_of_the_bar(self):
        steps = plan()
        by_key = {step.key: step.weight for step in steps}
        self.assertGreater(by_key["environments"], sum(w for k, w in by_key.items() if k != "environments") / 2)

    def test_finishing_fills_the_bar_and_marks_everything_done(self):
        run = progress.InstallProgress(plan())
        run.finish()
        self.assertEqual(run.fraction(), 1.0)
        self.assertEqual({row.mark for row in run.rows()}, {"done"})

    def test_a_failure_marks_the_running_step_and_keeps_the_rest_waiting(self):
        steps = plan()
        run = progress.InstallProgress(steps)
        run.feed(f"== {steps[0].label} ==")
        run.feed(f"== {steps[1].label} ==")
        run.fail()
        self.assertEqual([row.mark for row in run.rows()[:3]], ["done", "failed", "waiting"])

    def test_other_lines_become_the_detail_of_the_running_step_only(self):
        steps = plan()
        run = progress.InstallProgress(steps)
        run.feed("a line before any step")
        self.assertEqual({row.detail for row in run.rows()}, {""})
        run.feed(f"== {steps[0].label} ==")
        run.feed("Preparing AREG_IOS (7 of 19)")
        self.assertEqual(run.rows()[0].detail, "Preparing AREG_IOS (7 of 19)")
        run.feed(f"== {steps[1].label} ==")
        self.assertEqual(run.rows()[0].detail, "", "a finished step shows no detail")

    def test_a_long_line_is_cut_and_a_blank_one_ignored(self):
        steps = plan()
        run = progress.InstallProgress(steps)
        run.feed(f"== {steps[0].label} ==")
        run.feed("x" * 500)
        self.assertLessEqual(len(run.rows()[0].detail), 90)
        run.feed("   ")
        self.assertEqual(run.rows()[0].detail, "x" * 87 + "...")

    def test_a_plan_with_fewer_steps_is_resumed_not_restarted(self):
        """A second press leaves out what is done: the list shows only what is left."""
        resumed = backends.NATIVE.install_plan(fixtures.native(container={"running": False},
                                                              server={"healthy": False}))
        labels = [row.label for row in progress.InstallProgress(resumed).rows()]
        self.assertEqual(labels, ["Check the server version", "Start"])

    def test_a_line_naming_an_address_a_certificate_or_a_branch_is_not_shown(self):
        steps = plan()
        run = progress.InstallProgress(steps)
        run.feed(f"== {steps[-1].label} ==")
        for line in ("Serving at https://127.0.0.1:8000", "TLS certificate written",
                     "On branch feat/native-macos-server", "API token stored", "listening on localhost"):
            run.feed(line)
            self.assertEqual(run.rows()[-1].detail, "", line)
        run.feed("Waiting for the server to answer")
        self.assertEqual(run.rows()[-1].detail, "Waiting for the server to answer")

    def test_the_banner_of_an_unknown_step_changes_nothing(self):
        run = progress.InstallProgress(plan())
        run.feed("== something else ==")
        self.assertEqual({row.mark for row in run.rows()}, {"waiting"})


if __name__ == "__main__":
    unittest.main()
