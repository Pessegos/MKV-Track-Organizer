from __future__ import annotations

from pathlib import Path

import pytest

import makemkv_batch as makemkv


def test_selection_rule_for_known_modes() -> None:
    assert "+sel:(audio*eng)" in makemkv.selection_rule_for_mode("english")
    assert "+sel:audio" in makemkv.selection_rule_for_mode("all-audio")
    assert makemkv.selection_rule_for_mode("all-tracks").startswith("+sel:all")


def test_custom_selection_rule_must_not_be_empty() -> None:
    with pytest.raises(makemkv.MakeMkvError, match="empty"):
        makemkv.selection_rule_for_mode("custom", "")


def test_discover_disc_folders_accepts_source_root_as_disc(tmp_path: Path) -> None:
    source = tmp_path / "Movie Disc"
    (source / "BDMV").mkdir(parents=True)

    assert makemkv.discover_disc_folders(source) == [source.resolve()]


def test_discover_disc_folders_prefers_disc_like_children(tmp_path: Path) -> None:
    source = tmp_path / "season"
    (source / "disc2" / "BDMV").mkdir(parents=True)
    (source / "disc1" / "VIDEO_TS").mkdir(parents=True)
    (source / "notes").mkdir()

    assert [folder.name for folder in makemkv.discover_disc_folders(source)] == ["disc1", "disc2"]


def test_discover_disc_sources_accepts_single_iso(tmp_path: Path) -> None:
    source = tmp_path / "movie.iso"
    source.write_bytes(b"")

    assert makemkv.discover_disc_sources(source) == [source.resolve()]


def test_discover_disc_sources_finds_nested_iso_files(tmp_path: Path) -> None:
    source = tmp_path / "dvd set"
    (source / "movie b" / "nested").mkdir(parents=True)
    (source / "movie a" / "nested").mkdir(parents=True)
    iso_b = source / "movie b" / "nested" / "movie b.iso"
    iso_a = source / "movie a" / "nested" / "movie a.iso"
    mds_a = source / "movie a" / "nested" / "movie a.mds"
    iso_b.write_bytes(b"")
    iso_a.write_bytes(b"")
    mds_a.write_bytes(b"")

    assert [path.name for path in makemkv.discover_disc_sources(source)] == ["movie a.iso", "movie b.iso"]


def test_build_makemkv_command() -> None:
    command = makemkv.build_makemkv_command(
        Path(r"C:\MakeMKV\makemkvcon64.exe"),
        Path(r"D:\disc1"),
        Path(r"E:\out\disc1"),
        900,
    )

    assert command == [
        r"C:\MakeMKV\makemkvcon64.exe",
        "-r",
        "--minlength=900",
        "mkv",
        r"file:D:\disc1",
        "all",
        r"E:\out\disc1",
    ]


def test_build_makemkv_command_for_iso_uses_iso_source_and_stem_output() -> None:
    iso_path = Path(r"D:\DVDs\movie.iso")
    output_root = Path(r"E:\out")

    command = makemkv.build_makemkv_command(
        Path(r"C:\MakeMKV\makemkvcon64.exe"),
        iso_path,
        makemkv.output_folder_for_source(output_root, iso_path),
        900,
    )

    assert command[4] == r"iso:D:\DVDs\movie.iso"
    assert command[-1] == r"E:\out\movie"


def test_parse_robot_progress() -> None:
    assert makemkv.parse_robot_progress("PRGV:50,0,100") == (50, 100)
    assert makemkv.parse_robot_progress("PRGV:10,100,0") == (10, 100)
    assert makemkv.parse_robot_progress("MSG:1005,0,1") is None


def test_parse_robot_message_and_failure_summary() -> None:
    line = (
        'MSG:5021,131332,1,"This application version is too old.  Please download the latest version.",'
        '"template"'
    )

    assert makemkv.parse_robot_message(line) == "This application version is too old.  Please download the latest version."
    summary = makemkv.command_failure_message(
        makemkv.MakeMkvCommandResult(4294967293, [makemkv.parse_robot_message(line) or ""])
    )
    assert "too old" in summary
    assert "4294967293" in summary


def test_dry_run_batch_builds_reports_without_writing_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    makemkv_exe = tmp_path / "makemkvcon64.exe"
    (source / "disc1" / "BDMV").mkdir(parents=True)
    makemkv_exe.write_text("", encoding="utf-8")

    events = []
    result = makemkv.run_batch(
        makemkv.MakeMkvBatchJob(
            source_root=source,
            output_root=output,
            makemkv_path=makemkv_exe,
            dry_run=True,
        ),
        events.append,
    )

    assert result.return_code == 0
    assert result.reports[0]["status"] == "dry-run"
    assert not output.exists()
    assert [event.kind for event in events] == ["batch-started", "disc-started", "disc-finished", "batch-finished"]


def test_dry_run_batch_can_be_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    makemkv_exe = tmp_path / "makemkvcon64.exe"
    (source / "disc1" / "BDMV").mkdir(parents=True)
    makemkv_exe.write_text("", encoding="utf-8")

    result = makemkv.run_batch(
        makemkv.MakeMkvBatchJob(
            source_root=source,
            output_root=output,
            makemkv_path=makemkv_exe,
            dry_run=True,
        ),
        cancel_callback=lambda: True,
    )

    assert result.cancelled is True
    assert result.return_code == 130


def test_temporary_selection_rule_restores_existing_value(monkeypatch) -> None:
    writes = []
    monkeypatch.setattr(makemkv, "read_selection_rule", lambda: ("old-rule", True))
    monkeypatch.setattr(makemkv, "write_selection_rule", writes.append)
    monkeypatch.setattr(makemkv, "delete_selection_rule", lambda: pytest.fail("should not delete"))

    with makemkv.temporary_selection_rule("new-rule"):
        assert writes == ["new-rule"]

    assert writes == ["new-rule", "old-rule"]


def test_temporary_selection_rule_deletes_value_when_it_was_absent(monkeypatch) -> None:
    writes = []
    deletes = []
    monkeypatch.setattr(makemkv, "read_selection_rule", lambda: (None, False))
    monkeypatch.setattr(makemkv, "write_selection_rule", writes.append)
    monkeypatch.setattr(makemkv, "delete_selection_rule", lambda: deletes.append(True))

    with makemkv.temporary_selection_rule("new-rule"):
        assert writes == ["new-rule"]

    assert writes == ["new-rule"]
    assert deletes == [True]
