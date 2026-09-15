"""The package installs from a clean checkout.

Found by CI on the first push of a repository whose every other check was green:
all seven jobs died at `pip install -e ".[dev]"`. `dependencies = [...]` had
drifted below the `[project.urls]` header in pyproject.toml, so hatchling read
it as a URL and refused to build metadata. Nothing local noticed, because the
development environment was already installed and an editable install is never
re-resolved.

That is the general lesson, and the reason this file exists: a suite that runs
inside an environment cannot see whether that environment can be recreated. So
this checks the one thing the tests cannot otherwise reach -- that the metadata
hatchling would build from pyproject.toml is well-formed -- without a network,
a venv, or a subprocess install.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_dependencies_are_declared_in_the_project_table_not_a_subtable():
    """The exact drift CI caught. A TOML table runs until the next header."""
    project = _project()
    assert "dependencies" in project, (
        "project.dependencies is missing -- most likely it sits under a later "
        "header such as [project.urls], where a build backend reads it as a URL"
    )
    assert isinstance(project["dependencies"], list)
    assert all(isinstance(dep, str) for dep in project["dependencies"])
    assert "dependencies" not in project.get("urls", {})


def test_every_project_url_is_a_string_pointing_at_this_repository():
    for name, url in _project()["urls"].items():
        assert isinstance(url, str), f"project.urls.{name} is {type(url).__name__}, not a string"
        assert url.startswith("https://github.com/pjcampbe11/abCA"), (
            f"project.urls.{name} points somewhere other than this repository: {url}"
        )


def test_the_build_backend_accepts_the_metadata():
    """Ask hatchling itself, in-process. This is what `pip install` runs first."""
    hatchling = pytest.importorskip("hatchling.metadata.core")
    from hatchling.plugin.manager import PluginManager

    metadata = hatchling.ProjectMetadata(str(ROOT), PluginManager())
    metadata.validate_fields()          # raises on exactly the failure CI hit
    assert metadata.core.name == "abca"
    assert metadata.core.dependencies


def test_the_dev_extra_contains_what_ci_installs():
    extras = _project()["optional-dependencies"]
    assert "dev" in extras
    names = {dep.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip().lower()
             for dep in extras["dev"]}
    for tool in ("pytest", "pytest-cov", "ruff"):
        assert tool in names, f"CI runs {tool} but the dev extra does not install it"
