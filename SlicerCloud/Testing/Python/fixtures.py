"""Status dicts shaped like what `server_ctl.py status` prints, for the state tests.

Docker: the shape `FakeDeployment.status` of test_panel.py already uses and
`scripts/server_ctl.py` produces. Native: the keys of `cmd_status` in the
server's `scripts/server_ctl_native.py` (read there, not captured from a Mac:
no real output was available to this suite, so the values are invented and the
KEYS are the contract).
"""

import copy

GIB = 1024 ** 3


def docker(**over):
    status = {
        "cloned": True, "repo_root": "/home/u/SlicerCloudServer", "service": "inference-cpu",
        "git": {"available": True, "version": "git version 2.43.0"},
        "docker": {"available": True, "version": "Docker 27.3", "daemon": True,
                   "error": None, "needs_group": False},
        "compose": {"available": True, "version": "v2.29"},
        "gpu": {"nvidia_runtime": False, "nvidia_smi": False},
        "clone": {"is_git_repo": True, "branch": "main", "configured_branch": "main",
                  "commit": "abc1234", "behind": 0, "dirty": False, "checked_remote": False,
                  "branch_mismatch": False},
        "container": {"running": True, "state": "running"},
        "server": {"url": "https://localhost:8000", "healthy": True},
        "env": {"has_token": True},
    }
    return _merge(status, over)


def docker_uninstalled(**over):
    status = docker(cloned=False, clone={"is_git_repo": False, "behind": 0, "error": "not installed yet"},
                    container={"running": False, "state": None},
                    server={"url": "https://localhost:8000", "healthy": False},
                    env={"has_token": False})
    return _merge(status, over)


def native(**over):
    venvs = {"AMASSS": True, "ALI_CBCT": True, "ALI_IOS": True}
    status = {
        "backend": "inference-native", "cloned": True, "repo_root": "/Users/u/SlicerCloudServer",
        "git": {"available": True, "version": "git version 2.39.5"},
        "platform": {"system": "darwin", "machine": "arm64"},
        "uv": "/Users/u/.local/bin/uv", "service": "inference-native",
        "docker": {"available": False, "version": None, "daemon": False,
                   "error": "not used by the native backend"},
        "compose": {"available": False, "version": None},
        "gpu": {"nvidia_runtime": False, "nvidia_smi": False, "gpu_access": "mps", "error": None},
        "container": {"service": "inference-native", "exists": True, "running": True,
                      "state": "running", "error": None},
        "clone": {"is_git_repo": True, "branch": "main", "configured_branch": "main",
                  "commit": "91c87fe", "behind": 0, "dirty": False, "checked_remote": False,
                  "branch_mismatch": False},
        "process": {"pid": 4242, "running": True, "stale_pidfile": False},
        "url": "https://127.0.0.1:8000", "tls": {"enabled": True},
        "server": {"url": "https://127.0.0.1:8000", "healthy": True},
        "tools": {"sadt": "/Users/u/SADT-VISOR", "present": True, "venvs": venvs},
        "env": {"exists": True, "has_token": True},
    }
    return _merge(status, over)


def native_uninstalled(**over):
    """What `LocalServerDeployment.status()` answers before any clone."""
    status = {
        "native": True, "cloned": False, "repo_root": "/Users/u/SlicerCloudServer",
        "git": {"available": True, "version": "git version 2.39.5"},
        "uv": "/Users/u/.local/bin/uv",
        "docker": {"available": False, "version": None, "daemon": False,
                   "error": "not used by the native backend", "needs_group": False},
        "compose": {"available": False, "version": None},
        "gpu": {"nvidia_runtime": False, "nvidia_smi": False, "gpu_access": "mps"},
        "clone": {"is_git_repo": False, "behind": 0, "error": "not installed yet"},
        "container": {"running": False, "state": None},
        "server": {"url": "https://localhost:8000", "healthy": False},
        "env": {"has_token": False},
    }
    return _merge(status, over)


def _merge(base, over):
    base = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(copy.deepcopy(value))
        else:
            base[key] = copy.deepcopy(value)
    return base
