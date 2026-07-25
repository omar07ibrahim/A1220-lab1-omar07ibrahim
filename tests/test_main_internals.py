from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from receipt_extractor import file_io, gpt, main
from tests.test_cli import invoke, valid_receipt


def _changed_stat(details: os.stat_result, *, inode_delta: int = 0) -> os.stat_result:
    values = list(details)
    values[1] += inode_delta
    return os.stat_result(values)


def test_process_directory_supports_injected_and_default_adapters(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def extractor(image: file_io.ImagePayload) -> dict[str, Any]:
        calls.append(image.name)
        return valid_receipt(image, amount="$2.50")

    injected = main.process_directory(receipt_dir, extractor=extractor)
    monkeypatch.setattr(gpt, "extract_receipt_info", extractor)
    default = main.process_directory(receipt_dir)

    assert calls == ["A.jpg", "b.PNG", "A.jpg", "b.PNG"]
    assert injected == default
    assert all(value["amount"] == "$2.50" for value in injected.values())


def test_positive_integer_parser_and_direct_dry_run(receipt_dir: Path) -> None:
    assert main._positive_int("3") == 3
    with pytest.raises(argparse.ArgumentTypeError):
        main._positive_int("0")
    with pytest.raises(ValueError):
        main._positive_int("not-an-integer")

    dry_run = invoke(str(receipt_dir), "--dry-run")
    invalid_input = invoke(str(receipt_dir / "missing"), "--dry-run")
    assert dry_run.code == 0
    assert '"mode": "dry-run"' in dry_run.stdout
    assert invalid_input.code == 2
    assert "input validation failed" in invalid_input.stderr


def test_openai_boundary_returns_data_and_wraps_exceptions(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [image, *_] = file_io.load_images(receipt_dir)
    monkeypatch.setattr(
        gpt,
        "extract_receipt_info",
        lambda image: valid_receipt(image, amount="$4.00"),
    )
    assert main._openai_extract(image) == valid_receipt(image, amount="$4.00")

    def fail(_image: file_io.ImagePayload) -> dict[str, Any]:
        raise RuntimeError("provider-private-detail")

    monkeypatch.setattr(gpt, "extract_receipt_info", fail)
    with pytest.raises(main.ProviderExecutionError) as wrapped:
        main._openai_extract(image)
    assert "provider-private-detail" not in str(wrapped.value)


def test_output_path_contract_rejects_control_suffix_and_missing_parent(
    tmp_path: Path,
) -> None:
    with pytest.raises(main.OutputError, match="control or format"):
        main._reserve_private_output(tmp_path / "bad\u202e.json")
    with pytest.raises(main.OutputError, match=r"\.json"):
        main._reserve_private_output(tmp_path / "result.txt")
    with pytest.raises(main.OutputError, match="output parent"):
        main._reserve_private_output(tmp_path / "missing" / "result.json")


def test_commit_rejects_zero_write_and_changed_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zero_path = tmp_path / "zero.json"
    zero = main._reserve_private_output(zero_path)
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(main.OutputError, match="could not write"):
        zero.commit("{}")
    assert zero.close()
    assert not zero_path.exists()

    monkeypatch.undo()
    changed_path = tmp_path / "changed.json"
    changed = main._reserve_private_output(changed_path)
    real_fstat = os.fstat

    def wrong_size(descriptor: int) -> os.stat_result:
        details = real_fstat(descriptor)
        if descriptor == changed.file_descriptor:
            values = list(details)
            values[6] += 1
            return os.stat_result(values)
        return details

    monkeypatch.setattr(os, "fstat", wrong_size)
    with pytest.raises(main.OutputError, match="changed while"):
        changed.commit("{}")
    monkeypatch.setattr(os, "fstat", real_fstat)
    assert changed.close()
    assert not changed_path.exists()


@pytest.mark.parametrize("failure", ["missing-name", "wrong-inode"])
def test_commit_rejects_path_lookup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    output = tmp_path / f"{failure}.json"
    reservation = main._reserve_private_output(output)
    real_stat = os.stat

    def fail_named_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if (
            dir_fd == reservation.directory_descriptor
            and path == reservation.final_name
        ):
            if failure == "missing-name":
                raise OSError("injected named stat failure")
            return _changed_stat(
                real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks),
                inode_delta=1,
            )
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fail_named_stat)
    expected = "verify" if failure == "missing-name" else "path changed"
    with pytest.raises(main.OutputError, match=expected):
        reservation.commit("{}")
    monkeypatch.setattr(os, "stat", real_stat)
    assert reservation.close()
    assert not output.exists()


def test_directory_fsync_failure_retains_complete_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "durability.json"
    reservation = main._reserve_private_output(output)
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if descriptor == reservation.directory_descriptor:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(main.OutputError, match="durability is uncertain"):
        reservation.commit("{}")
    assert output.read_bytes() == b"{}\n"
    assert reservation.close()
    assert reservation.close()


@pytest.mark.parametrize("failure", ["stat", "unlink"])
def test_abort_cleanup_failures_are_reported_without_propagation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    output = tmp_path / f"cleanup-{failure}.json"
    reservation = main._reserve_private_output(output)
    real_stat = os.stat
    real_unlink = os.unlink

    def fail_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if (
            dir_fd == reservation.directory_descriptor
            and path == reservation.final_name
        ):
            raise OSError("injected cleanup stat failure")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def fail_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if (
            dir_fd == reservation.directory_descriptor
            and path == reservation.final_name
        ):
            raise OSError("injected cleanup unlink failure")
        real_unlink(path, dir_fd=dir_fd)

    if failure == "stat":
        monkeypatch.setattr(os, "stat", fail_stat)
    else:
        monkeypatch.setattr(os, "unlink", fail_unlink)
    assert not reservation.close()
    monkeypatch.setattr(os, "stat", real_stat)
    monkeypatch.setattr(os, "unlink", real_unlink)
    output.unlink()


def test_main_reports_output_error_and_cleanup_warning(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingOutput:
        def commit(self, _payload: str) -> None:
            raise main.OutputError("injected output failure")

        def close(self) -> bool:
            return False

    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    monkeypatch.setattr(main, "_reserve_private_output", lambda _path: FailingOutput())
    monkeypatch.setattr(
        main,
        "_openai_extract",
        lambda image: valid_receipt(image),
    )

    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        "result.json",
    )

    assert result.code == 1
    assert "injected output failure" in result.stderr
    assert "cleanup could not be confirmed" in result.stderr
