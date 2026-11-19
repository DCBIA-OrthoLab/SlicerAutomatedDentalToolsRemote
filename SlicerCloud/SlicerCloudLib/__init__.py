"""Support package for the Slicer Cloud module.

`deploy`, `backends`, `state` and `local_cert` live here, and none imports
`slicer` nor `qt`, see ARCHITECTURE.md's dependency rule and
`Testing/Python/test_deploy.py`, which runs them under plain
`python3 -m unittest`.
"""

from .deploy import (
    DEFAULT_BRANCH,
    DEFAULT_INSTALL_DIR,
    DEFAULT_REPO_URL,
    DEFAULT_SERVER_URL,
    DeploymentError,
    LocalServerDeployment,
    find_python,
    probe_host,
    select_backend,
)

__all__ = [
    "LocalServerDeployment",
    "DeploymentError",
    "probe_host",
    "select_backend",
    "find_python",
    "DEFAULT_REPO_URL",
    "DEFAULT_BRANCH",
    "DEFAULT_INSTALL_DIR",
    "DEFAULT_SERVER_URL",
]
