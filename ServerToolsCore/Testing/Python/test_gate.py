"""What a tool panel does when no server is set, and when the local one stopped.

The rules these tests pin, in the order a person meets them:

* the decision comes from the configured ADDRESS, never from the network: the
  placeholder host is recognised by name and no DNS query is made for it;
* a remote address is never touched: nothing starts because of it, whether or
  not it answers;
* only an address on this machine, with no answer, with a local install, starts
  the local server by itself.

The pure rules are tested directly; the card is tested against the Qt stand-ins
(what is only verifiable in Slicer is listed in ARCHITECTURE.md).
"""

import os
import socket
import sys
import threading
import unittest
from unittest import mock

_HERE = os.path.abspath(os.path.dirname(__file__))
_CORE = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _HERE)
sys.path.insert(0, _CORE)

import test_hosted_test_files as fixtures  # noqa: E402,F401 - installs the stubs

from ServerToolsCoreLib import gate  # noqa: E402

PLACEHOLDER = "https://set-your-server.invalid"
REMOTE = "https://lab-server.example.org"
LOCAL = "https://127.0.0.1:8000"


class TestClassify(unittest.TestCase):
    def test_the_placeholder_is_unset(self):
        self.assertEqual(gate.classify(PLACEHOLDER), gate.UNSET)
        self.assertEqual(gate.classify("https://anything.invalid:9"), gate.UNSET)
        self.assertEqual(gate.classify(""), gate.UNSET)
        self.assertEqual(gate.classify(None), gate.UNSET)

    def test_loopback_is_local_and_the_rest_is_ok(self):
        self.assertEqual(gate.classify(LOCAL), gate.LOCAL)
        self.assertEqual(gate.classify("https://[::1]:8000"), gate.LOCAL)
        self.assertEqual(gate.classify(REMOTE), gate.OK)
        self.assertEqual(gate.classify("https://100.83.47.100:8000"), gate.OK)

    def test_no_name_is_ever_resolved(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS query")), \
                mock.patch.object(socket, "gethostbyname", side_effect=AssertionError("DNS query")):
            for url in (PLACEHOLDER, REMOTE, LOCAL, "not a url", "https://[bad"):
                gate.classify(url)


class TestMayAutostart(unittest.TestCase):
    def test_a_local_address_that_does_not_answer_starts_the_local_install(self):
        self.assertTrue(gate.may_autostart(LOCAL, health_ok=False, local_installed=True))

    def test_a_server_that_answers_is_left_alone(self):
        self.assertFalse(gate.may_autostart(LOCAL, health_ok=True, local_installed=True))

    def test_nothing_is_installed_so_nothing_starts(self):
        self.assertFalse(gate.may_autostart(LOCAL, health_ok=False, local_installed=False))

    def test_a_remote_server_is_never_replaced_or_started_around(self):
        for url in (REMOTE, "https://100.83.47.100:8000", "https://server.tail1234.ts.net"):
            self.assertFalse(gate.may_autostart(url, health_ok=False, local_installed=True), url)

    def test_the_placeholder_starts_nothing_on_its_own(self):
        self.assertFalse(gate.may_autostart(PLACEHOLDER, health_ok=False, local_installed=True))


class TestSerializedStart(unittest.TestCase):
    def test_two_panels_never_start_the_server_at_the_same_time(self):
        inside = []
        overlap = []

        class Local:
            def start(self):
                inside.append(1)
                if len(inside) > 1:
                    overlap.append(True)
                threading.Event().wait(0.05)
                inside.pop()
                return {"url": LOCAL, "token": "t"}

        threads = [threading.Thread(target=gate.start_serialized, args=(Local(),)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(overlap, [])


class TestLocalWorkIsSharedWithSlicerCloud(unittest.TestCase):
    """An install, a start or an update running in the SlicerCloud panel and the
    automatic start of a tool panel must never both run `up`."""

    def test_idle_by_default_and_busy_between_begin_and_end(self):
        self.assertFalse(gate.local_work_active())
        token = gate.begin_local_work()
        self.assertTrue(gate.local_work_active())
        gate.end_local_work(token)
        self.assertFalse(gate.local_work_active())

    def test_ending_twice_is_harmless(self):
        token = gate.begin_local_work()
        gate.end_local_work(token)
        gate.end_local_work(token)
        self.assertFalse(gate.local_work_active())

    def test_two_jobs_keep_it_busy_until_both_end(self):
        first, second = gate.begin_local_work(), gate.begin_local_work()
        gate.end_local_work(first)
        self.assertTrue(gate.local_work_active())
        gate.end_local_work(second)
        self.assertFalse(gate.local_work_active())

    def test_waiting_returns_when_the_work_ends(self):
        token = gate.begin_local_work()
        threading.Timer(0.05, gate.end_local_work, args=(token,)).start()
        self.assertTrue(gate.wait_for_idle(timeout=2))

    def test_waiting_gives_up_after_the_timeout(self):
        token = gate.begin_local_work()
        self.addCleanup(gate.end_local_work, token)
        self.assertFalse(gate.wait_for_idle(timeout=0.05))

    def test_waiting_when_idle_is_immediate(self):
        self.assertTrue(gate.wait_for_idle(timeout=0))


if __name__ == "__main__":
    unittest.main()
