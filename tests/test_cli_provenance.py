from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from receipt_extractor import file_io, provenance, replay
from receipt_extractor.schema import ExpenseCategory
from tests.conftest import ImageFactory
from tests.test_cli import CliResult, invoke, write_replay_manifest

_VERIFY_STDOUT = (
    '{\n  "mode": "verify-run",\n  "schema_version": 1,\n  "verified": true\n}\n'
)
_VERIFY_FAILURE = (
    "run verification failed; details are suppressed to avoid leaking receipt data\n"
)


def _create_bundle(
    tmp_path: Path,
    receipt_dir: Path,
    *,
    output_name: str = "replay-run.json",
) -> tuple[Path, Path, bytes]:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    manifest_bytes = write_replay_manifest(manifest_path, images)
    run_path = tmp_path / output_name
    created = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--run-output",
        str(run_path),
    )
    assert created == CliResult(code=0, stdout="", stderr="")
    return manifest_path, run_path, manifest_bytes


@pytest.mark.parametrize(
    "arguments",
    [
        ("inputs", "--verify-run", "run.json"),
        (
            "inputs",
            "--verify-run",
            "run.json",
            "--against-manifest",
            "manifest.json",
            "--acknowledge-remote-upload",
        ),
        (
            "inputs",
            "--verify-run",
            "run.json",
            "--against-manifest",
            "manifest.json",
            "--output",
            "result.json",
        ),
        (
            "inputs",
            "--verify-run",
            "run.json",
            "--against-manifest",
            "manifest.json",
            "--stdout",
        ),
        (
            "inputs",
            "--verify-run",
            "run.json",
            "--against-manifest",
            "manifest.json",
            "--run-output",
            "result.json",
        ),
        ("inputs", "--dry-run", "--against-manifest", "manifest.json"),
        ("inputs", "--dry-run", "--run-output", "run.json"),
        (
            "inputs",
            "--replay",
            "replay.json",
            "--against-manifest",
            "manifest.json",
            "--stdout",
        ),
        (
            "inputs",
            "--replay",
            "replay.json",
            "--dry-run",
            "--stdout",
        ),
        (
            "inputs",
            "--replay",
            "replay.json",
            "--output",
            "result.json",
            "--run-output",
            "run.json",
        ),
        (
            "inputs",
            "--acknowledge-remote-upload",
            "--stdout",
            "--against-manifest",
            "manifest.json",
        ),
        (
            "inputs",
            "--acknowledge-remote-upload",
            "--run-output",
            "run.json",
        ),
    ],
)
def test_provenance_only_flags_fail_before_input_preflight(
    arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_load(*_args: object, **_kwargs: object) -> list[file_io.ImagePayload]:
        raise AssertionError("input preflight ran before CLI contract rejection")

    monkeypatch.setattr(file_io, "load_images", fail_load)

    result = invoke(*arguments)

    assert result.code == 2
    assert "usage:" in result.stderr


def test_bind_with_manifest_loads_once_and_keeps_bind_compatible(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    write_replay_manifest(manifest_path, images)
    real_load_manifest = replay.load_manifest
    loaded_paths: list[Path] = []

    def counted_load(path: Path) -> tuple[replay.ReplayManifest, str]:
        loaded_paths.append(path)
        return real_load_manifest(path)

    monkeypatch.setattr(replay, "load_manifest", counted_load)

    provider, manifest = replay.ReplayProvider.bind_with_manifest(
        manifest_path,
        images,
    )
    compatible = replay.ReplayProvider.bind(manifest_path, images)

    assert loaded_paths == [manifest_path, manifest_path]
    assert provider.items == compatible.items == tuple(manifest.batch.items)
    assert provider.manifest_sha256 == compatible.manifest_sha256


def test_run_creation_is_private_deterministic_typed_and_binds_manifest_once(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    document = json.loads(write_replay_manifest(manifest_path, images))
    document["batch"]["items"][0]["output"]["category"] = "Office Supplies"
    manifest_bytes = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ).encode("ascii")
    manifest_path.write_bytes(manifest_bytes)
    real_load_manifest = replay.load_manifest
    loaded_paths: list[Path] = []

    def counted_load(path: Path) -> tuple[replay.ReplayManifest, str]:
        loaded_paths.append(path)
        return real_load_manifest(path)

    monkeypatch.setattr(replay, "load_manifest", counted_load)
    first_path = tmp_path / "first-run.json"
    second_path = tmp_path / "second-run.json"

    first = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--run-output",
        str(first_path),
    )
    second = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--run-output",
        str(second_path),
    )

    assert first == second == CliResult(code=0, stdout="", stderr="")
    assert loaded_paths == [manifest_path, manifest_path]
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_path.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    assert first_path.stat().st_nlink == 1
    loaded = provenance.load_replay_run(first_path)
    assert [item.input_name for item in loaded.body.items] == [
        image.name for image in images
    ]
    assert loaded.body.items[0].output.category is ExpenseCategory.OFFICE_SUPPLIES
    assert loaded.body.replay_manifest_file_sha256 == (
        f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    )


def test_run_creation_preserves_legacy_replay_bytes(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    manifest_bytes = write_replay_manifest(manifest_path, images)
    output_path = tmp_path / "legacy-replay.json"

    replayed = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--output",
        str(output_path),
    )

    expected = {
        "mode": "replay",
        "receipts": {
            image.name: {
                "amount": f"${index + 1}.25",
                "category": "Other",
                "date": "2026-07-24",
                "vendor": f"Synthetic Vendor {index + 1}",
            }
            for index, image in enumerate(images)
        },
        "replay_manifest_sha256": (
            f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        ),
        "schema_version": 1,
    }
    expected_bytes = (
        json.dumps(
            expected,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")

    assert replayed == CliResult(code=0, stdout="", stderr="")
    assert output_path.read_bytes() == expected_bytes


def test_verify_run_prints_only_the_fixed_aggregate_summary(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    manifest_path, run_path, _ = _create_bundle(tmp_path, receipt_dir)

    verified = invoke(
        str(receipt_dir),
        "--verify-run",
        str(run_path),
        "--against-manifest",
        str(manifest_path),
    )

    assert verified == CliResult(code=0, stdout=_VERIFY_STDOUT, stderr="")
    assert "sha256:" not in verified.stdout
    assert all(
        image.name not in verified.stdout for image in file_io.load_images(receipt_dir)
    )


def test_verify_rejects_semantically_same_manifest_bytes_and_run_tampering(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    manifest_path, run_path, _ = _create_bundle(tmp_path, receipt_dir)
    equivalent_path = tmp_path / "equivalent-manifest.json"
    equivalent_path.write_text(
        json.dumps(
            json.loads(manifest_path.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="ascii",
    )
    assert equivalent_path.read_bytes() != manifest_path.read_bytes()
    tampered_path = tmp_path / "tampered-run.json"
    tampered: dict[str, Any] = json.loads(run_path.read_bytes())
    tampered["body"]["items"][0]["output"]["vendor"] = "PRIVATE-VENDOR-SENTINEL"
    tampered_path.write_text(
        json.dumps(
            tampered,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ),
        encoding="ascii",
    )

    equivalent = invoke(
        str(receipt_dir),
        "--verify-run",
        str(run_path),
        "--against-manifest",
        str(equivalent_path),
    )
    tampering = invoke(
        str(receipt_dir),
        "--verify-run",
        str(tampered_path),
        "--against-manifest",
        str(manifest_path),
    )

    assert (
        equivalent
        == tampering
        == CliResult(
            code=2,
            stdout="",
            stderr=_VERIFY_FAILURE,
        )
    )
    assert "PRIVATE-VENDOR-SENTINEL" not in tampering.stderr


def test_verify_input_failure_uses_the_same_path_redacted_error(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "PRIVATE-INPUT-PATH"

    result = invoke(
        str(private_path),
        "--verify-run",
        str(tmp_path / "PRIVATE-RUN.json"),
        "--against-manifest",
        str(tmp_path / "PRIVATE-MANIFEST.json"),
    )

    assert result == CliResult(code=2, stdout="", stderr=_VERIFY_FAILURE)
    assert "PRIVATE" not in result.stderr


def test_verify_rejects_a_valid_but_changed_current_image(
    tmp_path: Path,
    receipt_dir: Path,
    image_factory: ImageFactory,
) -> None:
    manifest_path, run_path, manifest_bytes = _create_bundle(tmp_path, receipt_dir)
    run_bytes = run_path.read_bytes()
    image_factory(
        receipt_dir / "b.PNG",
        "PNG",
        color="green",
        size=(4, 3),
    )

    result = invoke(
        str(receipt_dir),
        "--verify-run",
        str(run_path),
        "--against-manifest",
        str(manifest_path),
    )

    assert result == CliResult(code=2, stdout="", stderr=_VERIFY_FAILURE)
    assert "b.PNG" not in result.stderr
    assert "Traceback" not in result.stderr
    assert manifest_path.read_bytes() == manifest_bytes
    assert run_path.read_bytes() == run_bytes


def test_run_output_is_no_clobber_and_failed_build_cleans_reservation(
    tmp_path: Path,
    receipt_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    write_replay_manifest(manifest_path, images)
    existing_path = tmp_path / "existing-run.json"
    existing_path.write_bytes(b"preserve-this-sentinel")

    no_clobber = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--run-output",
        str(existing_path),
    )

    assert no_clobber.code == 1
    assert existing_path.read_bytes() == b"preserve-this-sentinel"

    failed_path = tmp_path / "failed-run.json"

    def fail_build(**_kwargs: object) -> provenance.ReplayRun:
        assert failed_path.exists()
        assert failed_path.stat().st_size == 0
        assert stat.S_IMODE(failed_path.stat().st_mode) == 0o600
        raise provenance.ProvenanceError("PRIVATE-BUILD-DETAIL")

    monkeypatch.setattr(provenance, "build_replay_run", fail_build)

    failed = invoke(
        str(receipt_dir),
        "--replay",
        str(manifest_path),
        "--run-output",
        str(failed_path),
    )

    assert failed.code == 1
    assert "replay execution failed; details are suppressed" in failed.stderr
    assert "PRIVATE-BUILD-DETAIL" not in failed.stderr
    assert not failed_path.exists()


def test_creation_and_verification_do_not_import_openai_or_require_a_key(
    tmp_path: Path,
    receipt_dir: Path,
) -> None:
    images = file_io.load_images(receipt_dir)
    manifest_path = tmp_path / "replay-manifest.json"
    write_replay_manifest(manifest_path, images)
    run_path = tmp_path / "subprocess-run.json"
    fake_module = tmp_path / "fake-provider"
    fake_module.mkdir()
    (fake_module / "openai.py").write_text(
        "raise AssertionError('OpenAI imported during provenance command')\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(fake_module), str(repository / "src")]
    )

    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.main",
            str(receipt_dir),
            "--replay",
            str(manifest_path),
            "--run-output",
            str(run_path),
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    verified = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.main",
            str(receipt_dir),
            "--verify-run",
            str(run_path),
            "--against-manifest",
            str(manifest_path),
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert created.returncode == 0
    assert created.stdout == created.stderr == ""
    assert verified.returncode == 0
    assert verified.stdout == _VERIFY_STDOUT
    assert verified.stderr == ""
