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

from receipt_extractor import file_io, main, replay


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


def write_replay_manifest(
    path: Path,
    images: list[file_io.ImagePayload],
) -> bytes:
    descriptors = [replay.descriptor_for(image) for image in images]
    manifest = {
        "kind": replay.REPLAY_KIND,
        "schema_version": 1,
        "batch": {
            "digest": replay.batch_digest(descriptors),
            "items": [
                {
                    "input": descriptor.model_dump(mode="json"),
                    "output": {
                        "date": "2026-07-24",
                        "amount": f"${index + 1}.25",
                        "vendor": f"Synthetic Vendor {index + 1}",
                        "category": "Other",
                    },
                }
                for index, descriptor in enumerate(descriptors)
            ],
        },
    }
    encoded = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ).encode("ascii")
    path.write_bytes(encoded)
    return encoded


def valid_receipt(
    image: file_io.ImagePayload | None = None,
    *,
    amount: str = "$1.00",
) -> dict[str, Any]:
    return {
        "date": "2026-07-24",
        "amount": amount,
        "vendor": image.name if image is not None else "Synthetic Vendor",
        "category": "Other",
    }


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


def test_replay_is_offline_exact_and_byte_deterministic(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest = tmp_path / "replay.json"
    write_replay_manifest(manifest, images)
    fake_module = tmp_path / "fake-provider"
    fake_module.mkdir()
    (fake_module / "openai.py").write_text(
        "raise AssertionError('OpenAI imported during replay')\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(fake_module), str(repository / "src")]
    )
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"

    outputs: list[bytes] = []
    for output in (first_output, second_output):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "receipt_extractor.main",
                str(receipt_dir),
                "--replay",
                str(manifest),
                "--output",
                str(output),
            ],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            timeout=5,
        )
        assert completed.returncode == 0
        assert completed.stdout == b""
        assert completed.stderr == b""
        outputs.append(output.read_bytes())

    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert payload["schema_version"] == 1
    assert payload["mode"] == "replay"
    assert payload["replay_manifest_sha256"].startswith("sha256:")
    assert list(payload["receipts"]) == ["A.jpg", "b.PNG"]
    assert [value["amount"] for value in payload["receipts"].values()] == [
        "$1.25",
        "$2.25",
    ]


def test_replay_mode_rejects_remote_flags_and_requires_sink(
    receipt_dir: Path,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "replay.json"
    write_replay_manifest(manifest, file_io.load_images(receipt_dir))

    no_sink = invoke(str(receipt_dir), "--replay", str(manifest))
    remote_ack = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest),
        "--acknowledge-remote-upload",
        "--stdout",
    )
    conflicting_mode = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest),
        "--dry-run",
        "--stdout",
    )

    assert [no_sink.code, remote_ack.code, conflicting_mode.code] == [2, 2, 2]


def test_mismatched_replay_fails_before_output_and_preserves_manifest(
    receipt_dir: Path,
    tmp_path: Path,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest = tmp_path / "replay.json"
    manifest_bytes = write_replay_manifest(manifest, list(reversed(images)))
    output = tmp_path / "must-not-exist.json"

    mismatch = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest),
        "--output",
        str(output),
    )
    same_path = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest),
        "--output",
        str(manifest),
    )

    assert mismatch.code == 2
    assert "details are suppressed" in mismatch.stderr
    assert not output.exists()
    assert same_path.code == 2
    assert manifest.read_bytes() == manifest_bytes


def test_replay_metadata_validation_is_redacted_at_the_cli(
    receipt_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [source, *_] = file_io.load_images(receipt_dir)
    manifest = tmp_path / "replay.json"
    write_replay_manifest(manifest, [source])
    invalid = file_io.ImagePayload(
        name="PRIVATE-METADATA.gif",
        media_type="image/gif",
        data=source.data,
        sha256=source.sha256,
        width=source.width,
        height=source.height,
    )
    monkeypatch.setattr(file_io, "load_images", lambda *_args, **_kwargs: [invalid])
    output = tmp_path / "must-not-exist.json"

    result = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest),
        "--output",
        str(output),
    )

    assert result.code == 2
    assert "replay validation failed; details are suppressed" in result.stderr
    assert "PRIVATE-METADATA" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_live_requires_acknowledgement_sink_and_key_before_provider(
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def provider(_image: file_io.ImagePayload) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return valid_receipt(_image)

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
        return valid_receipt(_image)

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
    first_payload = json.loads(first_bytes)
    assert first_payload["schema_version"] == 1
    assert first_payload["mode"] == "live"
    assert all(
        receipt["amount"] == "$12.50" for receipt in first_payload["receipts"].values()
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
        lambda image: valid_receipt(image, amount="$3.25"),
    )

    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--stdout",
    )

    assert result.code == 0 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["mode"] == "live"
    assert all(receipt["amount"] == "$3.25" for receipt in payload["receipts"].values())


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
        return valid_receipt(_image, amount="$5.00")

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


@pytest.mark.parametrize(
    "invalid",
    [
        {
            "date": "2026-07-24",
            "amount": 10**10000,
            "vendor": "\ud800",
            "category": "Other",
        },
        {
            "date": "2026-07-24",
            "amount": "$1.00",
            "vendor": "Synthetic",
            "category": "Other",
            "nested": float("nan"),
        },
        {
            "date": "2026-07-24",
            "amount": "$1.00",
            "vendor": "Synthetic",
            "category": "Other",
            "nested": object(),
        },
    ],
)
def test_malformed_provider_results_fail_the_typed_boundary(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: dict[str, Any],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-sentinel")
    output = tmp_path / "invalid.json"
    monkeypatch.setattr(main, "_openai_extract", lambda _image: invalid)

    result = invoke(
        str(receipt_dir),
        "--acknowledge-remote-upload",
        "--output",
        str(output),
    )

    assert result.code == 1
    assert "provider details are suppressed" in result.stderr
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
        return valid_receipt(_image)

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
        return valid_receipt(_image)

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
