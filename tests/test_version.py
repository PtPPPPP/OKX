from __future__ import annotations

import tomllib
from pathlib import Path

from app.version import APP_VERSION


def test_application_version_matches_project_metadata() -> None:
    with Path("pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    assert project["project"]["version"] == APP_VERSION
