"""The install screen's model: a list of steps and one weighted progress bar.

**This module imports neither `slicer` nor `qt`.** The install runs the steps of
`backend.install_plan()` in order, and each prints a banner line, `== <label> ==`,
before it starts (see `SlicerCloud.onInstall`). The panel feeds every line it
receives to `InstallProgress.feed`; the model says which step runs, which are
done, how far the whole install is, and what the running step last said.

The bar is weighted by the steps' own `weight` and moves when a step finishes:
no step reports a percentage, so none is invented, and no duration is promised.
"""

import collections
import re

_BANNER_OPEN = "== "
_BANNER_CLOSE = " =="
_DETAIL_MAX = 90

# A line that names an address, a certificate or a branch is not shown under a
# step: those are developer details, and the log has them.
_NOT_FOR_THIS_SCREEN = re.compile(r"https?://|\btls\b|certificate|branch|token|127\.0\.0\.1|localhost", re.I)

Row = collections.namedtuple("Row", "key label mark detail")


def banner(step) -> str:
    """The line a step prints before it runs; `feed` recognises exactly this."""
    return f"{_BANNER_OPEN}{step.label}{_BANNER_CLOSE}"


class InstallProgress:
    def __init__(self, steps):
        self._steps = list(steps)
        self._running = None      # index of the running step, None before the first
        self._finished = False
        self._failed = False
        self._detail = ""

    def feed(self, line: str) -> None:
        text = (line or "").strip()
        if text.startswith(_BANNER_OPEN) and text.endswith(_BANNER_CLOSE):
            label = text[len(_BANNER_OPEN):-len(_BANNER_CLOSE)]
            for index, step in enumerate(self._steps):
                if step.label == label:
                    self._running = index
                    self._detail = ""
                    return
            return
        if self._running is None or not text or _NOT_FOR_THIS_SCREEN.search(text):
            return
        self._detail = text if len(text) <= _DETAIL_MAX else text[:_DETAIL_MAX - 3] + "..."

    def finish(self) -> None:
        self._finished = True
        self._running = None
        self._detail = ""

    def fail(self) -> None:
        self._failed = True

    def rows(self) -> list:
        rows = []
        for index, step in enumerate(self._steps):
            if self._finished:
                mark = "done"
            elif self._running is None:
                mark = "waiting"
            elif index < self._running:
                mark = "done"
            elif index == self._running:
                mark = "failed" if self._failed else "running"
            else:
                mark = "waiting"
            detail = self._detail if mark in ("running", "failed") and index == self._running else ""
            rows.append(Row(step.key, step.label, mark, detail))
        return rows

    def fraction(self) -> float:
        if self._finished:
            return 1.0
        total = sum(step.weight for step in self._steps) or 1
        done = sum(step.weight for index, step in enumerate(self._steps)
                   if self._running is not None and index < self._running)
        return done / total
