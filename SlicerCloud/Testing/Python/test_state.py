"""The state of the local server, and the buttons each state offers.

    python3 -m unittest test_state

Pure functions, no widget, no Slicer: `state.classify` and `state.actions`. The
table at the end is the one the panel renders; a state added there without a
test row is a button nobody decided.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "ServerToolsCore"))
sys.path.insert(0, _HERE)

import fixtures  # noqa: E402
from SlicerCloudLib import backends, state  # noqa: E402
from SlicerCloudLib.state import State, actions, classify  # noqa: E402

DOCKER, NATIVE = backends.DOCKER, backends.NATIVE
LOCAL = "https://127.0.0.1:8000"
REMOTE = "https://lab.example.org"
UNSET = "https://set-your-server.invalid"
FREE = {"disk_free": 120 * fixtures.GIB, "port": 8000, "port_in_use": False}


def kind(status, backend, facts=None, job=None, client_url=LOCAL):
    return classify(status, backend, facts, job, client_url)


class TestDockerStates(unittest.TestCase):
    def test_docker_missing_needs_setup_and_names_the_remedy(self):
        status = fixtures.docker_uninstalled(docker={"available": False, "daemon": False})
        result = kind(status, DOCKER)
        self.assertEqual(result.kind, State.NEEDS_SETUP)
        self.assertEqual([c.key for c in result.missing], ["docker"])
        self.assertEqual(state.install_label(result, DOCKER), "Fix: Install Docker")

    def test_outside_the_docker_group_the_remedy_is_the_steps(self):
        status = fixtures.docker_uninstalled(docker={
            "available": True, "daemon": False, "needs_group": True, "error": "permission denied"})
        result = kind(status, DOCKER)
        self.assertEqual(result.kind, State.NEEDS_SETUP)
        self.assertEqual(state.install_label(result, DOCKER), "Fix: Show the steps")

    def test_nothing_installed(self):
        self.assertEqual(kind(fixtures.docker_uninstalled(), DOCKER).kind, State.NOT_INSTALLED)

    def test_installed_and_stopped(self):
        status = fixtures.docker(container={"running": False, "state": "exited"},
                                 server={"healthy": False})
        self.assertEqual(kind(status, DOCKER).kind, State.INSTALLED_STOPPED)

    def test_a_process_that_does_not_answer_yet_is_starting(self):
        status = fixtures.docker(server={"healthy": False})
        self.assertEqual(kind(status, DOCKER).kind, State.STARTING)

    def test_running(self):
        self.assertEqual(kind(fixtures.docker(), DOCKER).kind, State.RUNNING)

    def test_a_running_server_needs_no_prerequisite(self):
        """The probe may say anything once a container answers: it is up."""
        status = fixtures.docker(git={"available": False})
        self.assertEqual(kind(status, DOCKER).kind, State.RUNNING)

    def test_a_foreign_program_on_the_port(self):
        status = fixtures.docker(container={"running": False}, server={"healthy": False})
        facts = dict(FREE, port_in_use=True)
        self.assertEqual(kind(status, DOCKER, facts).kind, State.PORT_TAKEN)

    def test_a_folder_that_is_not_a_clone_is_broken(self):
        status = fixtures.docker(container={"running": False}, server={"healthy": False},
                                 clone={"is_git_repo": False, "error": "not a git clone"})
        self.assertEqual(kind(status, DOCKER).kind, State.BROKEN)

    def test_an_install_job_is_installing_whatever_the_status_says(self):
        self.assertEqual(kind(fixtures.docker_uninstalled(), DOCKER, job="install").kind,
                         State.INSTALLING)

    def test_another_job_does_not_change_the_state(self):
        result = kind(fixtures.docker(), DOCKER, job="update")
        self.assertEqual(result.kind, State.RUNNING)
        self.assertEqual(result.flags.busy, "update")

    def test_no_status_yet_is_not_a_running_server(self):
        self.assertNotEqual(kind(None, DOCKER).kind, State.RUNNING)

    def test_the_gpu_is_never_a_prerequisite(self):
        status = fixtures.docker_uninstalled(gpu={"nvidia_smi": True, "nvidia_runtime": False})
        self.assertEqual(kind(status, DOCKER).kind, State.NOT_INSTALLED)


class TestNativeStates(unittest.TestCase):
    def test_a_missing_uv_needs_setup_before_anything_is_cloned(self):
        status = fixtures.native_uninstalled(uv=None)
        result = kind(status, NATIVE, FREE)
        self.assertEqual(result.kind, State.NEEDS_SETUP)
        self.assertEqual([c.key for c in result.missing], ["uv"])
        self.assertEqual(state.install_label(result, NATIVE), "Fix: Install uv")

    def test_missing_git_and_uv_are_both_named_and_git_comes_first(self):
        status = fixtures.native_uninstalled(uv=None, git={"available": False, "version": None})
        result = kind(status, NATIVE, FREE)
        self.assertEqual([c.key for c in result.missing], ["git", "uv"])
        self.assertEqual(state.install_label(result, NATIVE), "Fix: Install the developer tools")

    def test_nothing_installed(self):
        self.assertEqual(kind(fixtures.native_uninstalled(), NATIVE, FREE).kind, State.NOT_INSTALLED)

    def test_cloned_but_tools_missing_is_still_not_installed(self):
        status = fixtures.native(container={"running": False}, server={"healthy": False},
                                 tools={"present": False, "venvs": {}})
        self.assertEqual(kind(status, NATIVE, FREE).kind, State.NOT_INSTALLED)

    def test_a_tool_environment_not_built_is_not_installed(self):
        status = fixtures.native(container={"running": False}, server={"healthy": False},
                                 tools={"venvs": {"AMASSS": True, "ALI_CBCT": False}})
        self.assertEqual(kind(status, NATIVE, FREE).kind, State.NOT_INSTALLED)

    def test_installed_and_stopped(self):
        status = fixtures.native(container={"running": False, "state": "exited"},
                                 server={"healthy": False})
        self.assertEqual(kind(status, NATIVE, FREE).kind, State.INSTALLED_STOPPED)

    def test_starting(self):
        self.assertEqual(kind(fixtures.native(server={"healthy": False}), NATIVE, FREE).kind,
                         State.STARTING)

    def test_running(self):
        self.assertEqual(kind(fixtures.native(), NATIVE, FREE).kind, State.RUNNING)

    def test_port_taken(self):
        status = fixtures.native(container={"running": False}, server={"healthy": False})
        self.assertEqual(kind(status, NATIVE, dict(FREE, port_in_use=True)).kind, State.PORT_TAKEN)

    def test_a_branch_other_than_the_one_asked_is_a_flag_not_a_state(self):
        status = fixtures.native(clone={"branch": "feat/x", "configured_branch": "main",
                                        "branch_mismatch": True})
        result = kind(status, NATIVE, FREE)
        self.assertEqual(result.kind, State.RUNNING)
        self.assertTrue(result.flags.branch_mismatch)
        self.assertTrue(result.flags.update_available)

    def test_behind_is_an_update(self):
        result = kind(fixtures.native(clone={"behind": 3}), NATIVE, FREE)
        self.assertTrue(result.flags.update_available)
        self.assertFalse(result.flags.branch_mismatch)


class TestWhereTheClientPoints(unittest.TestCase):
    def target(self, url, status=None, backend=NATIVE):
        return state.client_target(status or fixtures.native(), url, backend)

    def test_this_server_on_the_pinned_address(self):
        self.assertEqual(self.target(LOCAL), state.CLIENT_HERE)

    def test_another_port_on_this_machine_is_not_this_server(self):
        self.assertEqual(self.target("https://127.0.0.1:9000"), state.CLIENT_ELSEWHERE)

    def test_a_remote_server(self):
        self.assertEqual(self.target(REMOTE), state.CLIENT_ELSEWHERE)

    def test_no_server_chosen_yet(self):
        self.assertEqual(self.target(UNSET), state.CLIENT_UNSET)
        self.assertEqual(self.target(None), state.CLIENT_UNSET)
        self.assertEqual(self.target(""), state.CLIENT_UNSET)

    def test_the_deployments_own_public_address_is_here_for_docker(self):
        status = fixtures.docker(server={"public_url": "https://box.tail.ts.net"})
        self.assertEqual(self.target("https://box.tail.ts.net/", status, DOCKER), state.CLIENT_HERE)

    def test_a_malformed_url_is_somewhere_else(self):
        self.assertEqual(self.target("https://[bad"), state.CLIENT_ELSEWHERE)


# What the panel shows for each state. The strings are the widgets' names.
TABLE = [
    # label, status, backend, job, client, expected primary, shown
    ("docker: docker missing", fixtures.docker_uninstalled(docker={"available": False, "daemon": False}),
     DOCKER, None, UNSET, "install", {"install", "refresh"}),
    ("docker: not installed", fixtures.docker_uninstalled(), DOCKER, None, UNSET,
     "install", {"install", "refresh"}),
    ("docker: installing", fixtures.docker_uninstalled(), DOCKER, "install", UNSET, None, {"cancel"}),
    ("docker: stopped", fixtures.docker(container={"running": False}, server={"healthy": False}),
     DOCKER, None, UNSET, "install", {"install", "refresh", "logs"}),
    ("docker: stopped, behind",
     fixtures.docker(container={"running": False}, server={"healthy": False}, clone={"behind": 2}),
     DOCKER, None, UNSET, "install", {"install", "update", "refresh", "logs"}),
    ("docker: starting", fixtures.docker(server={"healthy": False}), DOCKER, None, UNSET,
     None, {"stop", "refresh", "logs"}),
    ("docker: running, client unset", fixtures.docker(), DOCKER, None, UNSET,
     "use_here", {"use_here", "stop", "refresh", "logs"}),
    ("docker: running, client here",
     fixtures.docker(server={"public_url": "https://box.ts.net"}), DOCKER, None, "https://box.ts.net",
     "stop", {"stop", "refresh", "logs"}),
    ("docker: running, client elsewhere", fixtures.docker(), DOCKER, None, REMOTE,
     "use_here", {"use_here", "stop", "refresh", "logs"}),
    ("native: uv missing", fixtures.native_uninstalled(uv=None), NATIVE, None, UNSET,
     "install", {"install", "refresh"}),
    ("native: not installed", fixtures.native_uninstalled(), NATIVE, None, UNSET,
     "install", {"install", "refresh"}),
    ("native: stopped", fixtures.native(container={"running": False}, server={"healthy": False}),
     NATIVE, None, LOCAL, "install", {"install", "refresh", "logs"}),
    ("native: running, client here", fixtures.native(), NATIVE, None, LOCAL,
     "stop", {"stop", "refresh", "logs"}),
    ("native: running, client here, update",
     fixtures.native(clone={"behind": 1}), NATIVE, None, LOCAL,
     "stop", {"stop", "update", "refresh", "logs"}),
    ("native: running, branch differs",
     fixtures.native(clone={"branch": "feat/x", "branch_mismatch": True}), NATIVE, None, LOCAL,
     "stop", {"stop", "update", "refresh", "logs"}),
    ("native: running, client elsewhere", fixtures.native(), NATIVE, None, REMOTE,
     "use_here", {"use_here", "stop", "refresh", "logs"}),
    ("native: port taken",
     fixtures.native(container={"running": False}, server={"healthy": False}),
     NATIVE, None, LOCAL, "try_port", {"try_port", "refresh", "logs"}),
    ("native: broken",
     fixtures.native(container={"running": False}, server={"healthy": False},
                     clone={"is_git_repo": False}),
     NATIVE, None, LOCAL, None, {"refresh", "logs"}),
]


class TestTheButtonsEachStateOffers(unittest.TestCase):
    def test_the_table(self):
        for label, status, backend, job, client, primary, shown in TABLE:
            facts = dict(FREE, port_in_use=label.endswith("port taken"))
            current = classify(status, backend, facts, job, client)
            result = actions(current)
            with self.subTest(label):
                self.assertEqual(result.primary, primary)
                self.assertEqual(set(result.shown), shown)

    def test_start_is_never_offered_while_the_server_runs(self):
        for backend, status in ((DOCKER, fixtures.docker()), (NATIVE, fixtures.native())):
            for client in (LOCAL, REMOTE, UNSET):
                current = classify(status, backend, FREE, None, client)
                self.assertNotIn("install", actions(current).shown)

    def test_only_cancel_is_shown_while_installing(self):
        current = classify(fixtures.native_uninstalled(), NATIVE, FREE, "install", UNSET)
        self.assertEqual(set(actions(current).shown), {"cancel"})

    def test_the_gpu_link_is_only_for_a_card_that_can_be_fixed(self):
        current = classify(fixtures.docker_uninstalled(), DOCKER, None, None, UNSET)
        self.assertNotIn("gpu", actions(current).shown)
        self.assertIn("gpu", actions(current, gpu_fixable=True).shown)
        busy = classify(fixtures.docker_uninstalled(), DOCKER, None, "install", UNSET)
        self.assertNotIn("gpu", actions(busy, gpu_fixable=True).shown)

    def test_switch_back_needs_a_kept_address_and_the_client_here(self):
        here = classify(fixtures.native(), NATIVE, FREE, None, LOCAL)
        self.assertIn("switch_back", actions(here, can_switch_back=True).shown)
        self.assertNotIn("switch_back", actions(here).shown)
        elsewhere = classify(fixtures.native(), NATIVE, FREE, None, REMOTE)
        self.assertNotIn("switch_back", actions(elsewhere, can_switch_back=True).shown)

    def test_labels_follow_the_state_and_the_backend(self):
        stopped = classify(fixtures.native(container={"running": False}, server={"healthy": False}),
                           NATIVE, FREE, None, LOCAL)
        self.assertEqual(state.install_label(stopped, NATIVE), "Start VISOR Local")
        fresh = classify(fixtures.native_uninstalled(), NATIVE, FREE, None, UNSET)
        self.assertEqual(state.install_label(fresh, NATIVE), "Set up VISOR Local")
        docker_fresh = classify(fixtures.docker_uninstalled(), DOCKER, None, None, UNSET)
        self.assertEqual(state.install_label(docker_fresh, DOCKER), "Install and start")
        docker_stopped = classify(fixtures.docker(container={"running": False}), DOCKER, None, None, UNSET)
        self.assertEqual(state.install_label(docker_stopped, DOCKER), "Start server")


if __name__ == "__main__":
    unittest.main()
