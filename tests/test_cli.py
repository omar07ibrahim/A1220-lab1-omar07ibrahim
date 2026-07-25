from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from receipt_extractor import file_io, main, postprocess


@dataclass(frozen=True, slots=True)
class CliResult:
    code: int
    stdout: str
    stderr: str


def invoke(*arguments: str) -> CliResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main.main(arguments)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 1
    return CliResult(code=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def test_help_and_dry_run_do_not_import_provider_or_require_key(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake_module = tmp_path / "fake-provider"
    fake_module.mkdir()
    (fake_module / "openai.py").write_text(
        "raise AssertionError('OpenAI imported during offline command')\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(fake_module), str(repository / "src")]
    )

    help_run = subprocess.run(
        [sys.executable, "-m", "receipt_extractor.main", "--help"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    dry_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.main",
            str(receipt_dir),
            "--dry-run",
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert help_run.returncode == 0
    assert "acknowledge-remote-upload" in help_run.stdout
    assert dry_run.returncode == 0
    payload = json.loads(dry_run.stdout)
    assert [image["name"] for image in payload["images"]] == ["A.jpg", "b.PNG"]
    assert dry_run.stderr == ""


def test_live_requires_acknowledgement_sink_and_key_before_provider(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"amount": "1.00"}

    monkeypatch.setattr(main, "_openai_extract", provider)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    missing_ack = invoke(str(receipt_dir), "--stdout")
    missing_sink = invoke(str(receipt_dir), "--acknowledge-remote-upload")
    missing_key = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--stdout",
    )
    conflicting = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--stdout",
        "--output",
        "result.json",
    )

    assert [
        missing_ack.code,
        missing_sink.code,
        missing_key.code,
        conflicting.code,
    ] == [
        2,
        2,
        2,
        2,
    ]
    assert calls == 0


def test_existing_output_kinds_are_preserved_before_provider(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    calls = 0

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"amount": "1.00"}

    monkeypatch.setattr(main, "_openai_extract", provider)
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve-me")
    regular = tmp_path / "regular.json"
    regular.write_bytes(b"regular")
    hardlink = tmp_path / "hardlink.json"
    os.link(victim, hardlink)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(victim)

    for output in (regular, hardlink, symlink):
        result = invoke(
            str(receipt_dir),
            "--acknowledge-remote-upload",
            "--output",
            str(output),
        )
        assert result.code == 1
        assert "already exists" in result.stderr

    assert regular.read_bytes() == b"regular"
    assert victim.read_bytes() == b"preserve-me"
    assert hardlink.read_bytes() == b"preserve-me"
    assert hardlink.stat().st_nlink == 2
    assert symlink.is_symlink()
    assert calls == 0


def test_fifo_output_never_blocks_or_imports_provider(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    fifo = tmp_path / "result.json"
    os.mkfifo(fifo)
    fake_module = tmp_path / "fake-provider"
    fake_module.mkdir()
    (fake_module / "openai.py").write_text(
        "raise AssertionError('provider imported before output preflight')\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = "offline-sentinel"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(fake_module), str(repository / "src")]
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.main",
            str(receipt_dir),
            "--acknowledge-remote-upload",
            "--output",
            str(fifo),
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 1
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert "offline-sentinel" not in completed.stdout + completed.stderr


def test_private_output_is_complete_and_cannot_be_reused(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    output = tmp_path / "result.json"
    calls = 0

    def provider(image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert output.exists()
        assert output.stat().st_size == 0
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        return {
            "date": "2026-07-24",
            "amount": "$12.50",
            "vendor": image.name,
            "category": "Other",
        }

    monkeypatch.setattr(main, "_openai_extract", provider)
    first = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )
    first_bytes = output.read_bytes()
    second = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert first.code == 0 and first.stdout == "" and first.stderr == ""
    assert first_bytes.endswith(b"\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1
    assert all(
        receipt["amount"] == 12.5 for receipt in json.loads(first_bytes).values()
    )
    assert second.code == 1
    assert output.read_bytes() == first_bytes
    assert calls == 2


def test_stdout_is_an_explicit_live_sink(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    monkeypatch.setattr(
        main,
        "_openai_extract",
        lambda image: {"amount": "3.25", "vendor": image.name},
    )

    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--stdout",
    )

    assert result.code == 0 and result.stderr == ""
    assert all(
        receipt["amount"] == 3.25 for receipt in json.loads(result.stdout).values()
    )


def test_provider_failure_is_redacted_and_reservation_is_removed(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_sentinel = "KEY-MUST-NOT-LEAK"
    receipt_sentinel = "RECEIPT-MUST-NOT-LEAK"
    monkeypatch.setenv("OPENAI_API_KEY", key_sentinel)
    output = tmp_path / "failed.json"

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        assert output.exists()
        raise main.ProviderExecutionError(f"{key_sentinel}:{receipt_sentinel}")

    monkeypatch.setattr(main, "_openai_extract", provider)
    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert result.code == 1
    assert "provider details are suppressed" in result.stderr
    assert key_sentinel not in result.stdout + result.stderr
    assert receipt_sentinel not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_second_provider_failure_never_leaves_partial_results(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    output = tmp_path / "batch.json"
    calls = 0

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise main.ProviderExecutionError
        return {"amount": "5.00"}

    monkeypatch.setattr(main, "_openai_extract", provider)
    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert result.code == 1
    assert calls == 2
    assert not output.exists()


def test_nonfinite_huge_and_unserializable_results_fail_closed(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    huge_output = tmp_path / "huge.json"

    def huge_result(_image: file_io.ImagePayload) -> dict[str, Any]:
        return {"amount": 10**10000, "vendor": "\ud800"}

    monkeypatch.setattr(main, "_openai_extract", huge_result)
    huge = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(huge_output),
    )
    huge_payload = json.loads(huge_output.read_text(encoding="ascii"))
    assert huge.code == 0
    assert all(
        value["amount"] is None and value["vendor"] == "\ud800"
        for value in huge_payload.values()
    )
    assert b"\\ud800" in huge_output.read_bytes()

    for index, invalid in enumerate((float("nan"), object())):
        output = tmp_path / f"invalid-{index}.json"

        def invalid_result(
            _image: file_io.ImagePayload,
            value: object = invalid,
        ) -> dict[str, Any]:
            return {"amount": "1.00", "nested": value}

        monkeypatch.setattr(main, "_openai_extract", invalid_result)
        result = invoke(
            str(receipt_dir),
            "--acknowledge-remote-upload",
            "--output",
            str(output),
        )
        assert result.code == 1
        assert "serialization failed" in result.stderr
        assert "Traceback" not in result.stderr
        assert not output.exists()


@pytest.mark.parametrize("failure_point", ["fstat", "fchmod"])
def test_reservation_failures_close_fd_and_remove_path(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    output = tmp_path / f"{failure_point}.json"
    provider_calls = 0
    captured_descriptor = -1
    real_fstat = os.fstat
    real_fchmod = os.fchmod

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        return {"amount": "1.00"}

    def fail_file_fstat(descriptor: int) -> os.stat_result:
        nonlocal captured_descriptor
        descriptor_path = Path(f"/proc/self/fd/{descriptor}")
        if descriptor_path.exists() and descriptor_path.resolve() == output:
            captured_descriptor = descriptor
            raise OSError("injected file fstat failure")
        return real_fstat(descriptor)

    def fail_fchmod(descriptor: int, mode: int) -> None:
        nonlocal captured_descriptor
        captured_descriptor = descriptor
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(main, "_openai_extract", provider)
    if failure_point == "fstat":
        monkeypatch.setattr(os, "fstat", fail_file_fstat)
    else:
        monkeypatch.setattr(os, "fchmod", fail_fchmod)

    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert result.code == 1
    assert "output failed" in result.stderr
    assert provider_calls == 0
    assert not output.exists()
    with pytest.raises(OSError) as closed:
        real_fstat(captured_descriptor)
    assert closed.value.errno == errno.EBADF
    monkeypatch.setattr(os, "fchmod", real_fchmod)


def test_untrusted_parent_rejects_before_provider(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o770)
    unsafe.chmod(0o770)
    output = unsafe / "result.json"
    calls = 0

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"amount": "1.00"}

    monkeypatch.setattr(main, "_openai_extract", provider)
    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert result.code == 1
    assert "not group- or world-writable" in result.stderr
    assert calls == 0
    assert not output.exists()


def test_output_can_only_be_committed_once(tmp_path: Path) -> None:
    output = tmp_path / "once.json"
    reservation = main._reserve_private_output(output)
    reservation.commit("{}")
    first = output.read_bytes()

    with pytest.raises(main.OutputError, match="no longer writable"):
        reservation.commit('{"changed": true}')

    assert output.read_bytes() == first
    assert reservation.close()


@pytest.mark.parametrize(
    "path",
    [
        "bad-\ud800.json",
        "bad-\ud800-parent/result.json",
    ],
)
def test_unencodable_output_paths_fail_without_traceback(
    tmp_path: Path,
    receipt_dir: Path,
    path: str,
) -> None:
    result = invoke(
        str(receipt_dir),
        "--dry-run",
        "--output",
        str(tmp_path / path),
    )

    assert result.code == 1
    assert "output failed" in result.stderr
    assert "Traceback" not in result.stderr


def test_amount_normalization_rejects_nonfinite_and_huge_values() -> None:
    for amount in (
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        "1e9999",
        "not-an-amount",
        10**10000,
        True,
        object(),
    ):
        assert postprocess.normalize_amount({"amount": amount})["amount"] is None
    assert postprocess.normalize_amount({"amount": "$ 12.50 "})["amount"] == 12.5
    assert postprocess.normalize_amount({"amount": None})["amount"] is None


def test_make_dry_run_never_expands_key_value() -> None:
    repository = Path(__file__).resolve().parents[1]
    sentinel = "MAKE-KEY-MUST-NOT-APPEAR"
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = sentinel

    completed = subprocess.run(
        ["make", "-n", "run"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert sentinel not in completed.stdout + completed.stderr
