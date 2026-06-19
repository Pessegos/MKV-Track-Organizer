from __future__ import annotations

import re
from pathlib import Path

import pytest

import app_metadata
import mkv_track_organizer as organizer


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_semantic_and_documented() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", app_metadata.APP_VERSION)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    release_notes = ROOT / ".github" / "release-notes" / f"v{app_metadata.APP_VERSION}.md"

    assert f"## {app_metadata.APP_VERSION} " in changelog
    assert app_metadata.APP_VERSION in readme
    assert release_notes.is_file()
    assert "still a work in progress" not in readme.casefold()


def test_cli_reports_the_central_application_version(capsys) -> None:
    parser = organizer.build_parser()

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--version"])

    assert exit_info.value.code == 0
    assert app_metadata.APP_VERSION in capsys.readouterr().out


def test_packaging_uses_version_metadata_and_smoke_test() -> None:
    build_script = (ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "--version-file" in build_script
    assert "app_metadata" in build_script
    assert "smoke_test_exe.ps1" in workflow
    assert "actions/checkout@v5" in workflow
    assert "actions/setup-python@v6" in workflow
