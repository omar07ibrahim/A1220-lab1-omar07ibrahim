from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from receipt_extractor import file_io, provenance, replay

REPOSITORY = Path(__file__).resolve().parents[1]
DEMO = REPOSITORY / "demo"
ASSETS = REPOSITORY / "docs" / "assets"
EXPECTED_DEMO_FILES = {
    "evidence/coverage-summary.json",
    "evidence/dry-run.json",
    "evidence/failure-paths.json",
    "evidence/help.txt",
    "evidence/provenance-source.json",
    "evidence/replay-result.json",
    "evidence/replay-run.json",
    "evidence/run-verification.json",
    "failures/corrupt-batch/01-valid.png",
    "failures/corrupt-batch/02-corrupt.png",
    "failures/existing-output.json",
    "failures/provider-sentinel/openai.py",
    "failures/reversed-replay-manifest.json",
    "inputs/cafe-lumen.png",
    "inputs/metro-line.webp",
    "replay-manifest.json",
}
EXPECTED_ASSET_FILES = {
    "architecture.svg",
    "cli-dry-run.png",
    "cli-help.png",
    "cli-provenance.png",
    "cli-replay.png",
    "coverage.png",
    "coverage.svg",
    "demo-receipts.png",
    "demo.gif",
    "failure-boundaries.png",
    "provenance-bindings.svg",
}
EXPECTED_SOURCE_FILES = {
    "MANIFEST.in",
    "Makefile",
    "README.md",
    "docs/run-provenance.md",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "scripts/capture_demo.py",
    "src/receipt_extractor/__init__.py",
    "src/receipt_extractor/artifact_io.py",
    "src/receipt_extractor/file_io.py",
    "src/receipt_extractor/gpt.py",
    "src/receipt_extractor/main.py",
    "src/receipt_extractor/provenance.py",
    "src/receipt_extractor/replay.py",
    "src/receipt_extractor/schema.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/test_artifact_io.py",
    "tests/test_cli.py",
    "tests/test_cli_provenance.py",
    "tests/test_demo_evidence.py",
    "tests/test_file_io.py",
    "tests/test_gpt.py",
    "tests/test_main_internals.py",
    "tests/test_provenance.py",
    "tests/test_replay.py",
    "tests/test_schema.py",
}


def _json_object(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return cast(dict[str, Any], decoded)


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_tracked_synthetic_inputs_pass_preflight_and_bind_exactly() -> None:
    images = file_io.load_images(DEMO / "inputs")
    manifest, _ = replay.load_manifest(DEMO / "replay-manifest.json")

    assert [image.name for image in images] == [
        "cafe-lumen.png",
        "metro-line.webp",
    ]
    assert [replay.descriptor_for(image) for image in images] == [
        item.input for item in manifest.batch.items
    ]

    provider = replay.ReplayProvider.bind(DEMO / "replay-manifest.json", images)
    for image in images:
        provider(image)
    provider.finalize()


def test_replay_evidence_matches_bound_provider_and_raw_manifest_sha() -> None:
    manifest_path = DEMO / "replay-manifest.json"
    images = file_io.load_images(DEMO / "inputs")
    provider = replay.ReplayProvider.bind(manifest_path, images)
    receipts = {image.name: provider(image) for image in images}
    provider.finalize()

    raw_manifest_sha = (
        f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
    )
    expected = {
        "mode": "replay",
        "receipts": receipts,
        "replay_manifest_sha256": raw_manifest_sha,
        "schema_version": 1,
    }

    assert provider.manifest_sha256 == raw_manifest_sha
    assert _json_object(DEMO / "evidence" / "replay-result.json") == expected


def test_unchanged_demo_artifacts_keep_their_reviewed_bytes() -> None:
    expected = {
        "demo/inputs/cafe-lumen.png": (
            "sha256:1a9d5c5d4d1f9e2ec4b6d5b2b9a8006efac7f044c4011f7648a9311ae7071a35"
        ),
        "demo/inputs/metro-line.webp": (
            "sha256:d3a2f4f45454b2870779a040421fcee7527c8c2b72ee92024f99d26f5b68bae0"
        ),
        "demo/replay-manifest.json": (
            "sha256:f994e398fe79f575daefbd566feb37c4b9dfcbe1db7838bb6218630b072d04d5"
        ),
        "demo/evidence/replay-result.json": (
            "sha256:0caca43e535512c10f50813cebd4b7b28efbc8ff652b37dae7c14b6d47922c96"
        ),
        "demo/evidence/dry-run.json": (
            "sha256:2897a837e8d065dfa4d66c018c71110c1878968ce0e5fa5fef28ae7bbc4f76a6"
        ),
        "demo/evidence/failure-paths.json": (
            "sha256:dfd6e40ba30d85515518c3796a3abb371708d5949ad0a3949b37320d04d5b2e9"
        ),
        "docs/assets/demo-receipts.png": (
            "sha256:1112538c4bfb90cf6fa822fed677bb61f575a70ffd8644f50f64a4e9a7431af3"
        ),
        "docs/assets/cli-dry-run.png": (
            "sha256:6c6558ce4befffaa1bb83005211eac72522124a287cace22a407127b9188de38"
        ),
        "docs/assets/cli-replay.png": (
            "sha256:e5e1eecc65ba109f69586fdd39e9b91cd64de33f217ab6d0c93dbd96d52fb7ff"
        ),
        "docs/assets/failure-boundaries.png": (
            "sha256:5123336516a8681cad83a9879b169deba452e21d9c382e5c3e3afdba2404433b"
        ),
    }
    assert {
        logical_path: _sha256(REPOSITORY / logical_path) for logical_path in expected
    } == expected


def test_provenance_evidence_reexecutes_with_poison_provider_and_no_key(
    tmp_path: Path,
) -> None:
    source_path = DEMO / "evidence" / "provenance-source.json"
    source = _json_object(source_path)
    run_path = DEMO / "evidence" / "replay-run.json"
    verification_path = DEMO / "evidence" / "run-verification.json"
    run_bytes = run_path.read_bytes()
    verification_bytes = verification_path.read_bytes()
    scratch_demo = tmp_path / "demo"
    scratch_evidence = scratch_demo / "evidence"
    scratch_evidence.mkdir(parents=True, mode=0o700)
    shutil.copytree(DEMO / "inputs", scratch_demo / "inputs")
    shutil.copytree(
        DEMO / "failures" / "provider-sentinel",
        scratch_demo / "failures" / "provider-sentinel",
    )
    shutil.copy2(DEMO / "replay-manifest.json", scratch_demo)

    commands = cast(dict[str, dict[str, Any]], source["commands"])
    create = commands["create"]
    verify = commands["verify"]
    logical_create = [
        "python",
        "-m",
        "receipt_extractor.main",
        "demo/inputs",
        "--replay",
        "demo/replay-manifest.json",
        "--run-output",
        "demo/evidence/replay-run.json",
    ]
    logical_verify = [
        "python",
        "-m",
        "receipt_extractor.main",
        "demo/inputs",
        "--verify-run",
        "demo/evidence/replay-run.json",
        "--against-manifest",
        "demo/replay-manifest.json",
    ]
    assert create["argv"] == logical_create
    assert verify["argv"] == logical_verify
    assert create["normalized"] == (
        "PYTHONPATH=demo/failures/provider-sentinel:src " + shlex.join(logical_create)
    )
    assert verify["normalized"] == (
        "PYTHONPATH=demo/failures/provider-sentinel:src " + shlex.join(logical_verify)
    )

    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(scratch_demo / "failures" / "provider-sentinel"),
            str(REPOSITORY / "src"),
        )
    )

    actual_create = [
        sys.executable,
        "-m",
        "receipt_extractor.main",
        str(scratch_demo / "inputs"),
        "--replay",
        str(scratch_demo / "replay-manifest.json"),
        "--run-output",
        str(scratch_evidence / "replay-run.json"),
    ]
    completed_create = subprocess.run(
        actual_create,
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    scratch_run = scratch_evidence / "replay-run.json"
    details = scratch_run.lstat()
    assert completed_create.returncode == create["exit_code"] == 0
    assert completed_create.stdout == ""
    assert completed_create.stderr == ""
    assert stat.S_ISREG(details.st_mode)
    assert details.st_nlink == 1
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert scratch_run.read_bytes() == run_bytes

    actual_verify = [
        sys.executable,
        "-m",
        "receipt_extractor.main",
        str(scratch_demo / "inputs"),
        "--verify-run",
        str(scratch_run),
        "--against-manifest",
        str(scratch_demo / "replay-manifest.json"),
    ]
    completed_verify = subprocess.run(
        actual_verify,
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed_verify.returncode == verify["exit_code"] == 0
    assert completed_verify.stdout.encode("ascii") == verification_bytes
    assert completed_verify.stderr == ""

    empty_sha = f"sha256:{hashlib.sha256(b'').hexdigest()}"
    assert create["stdout"] == {
        "artifact": None,
        "bytes": 0,
        "sha256": empty_sha,
    }
    assert create["stderr"] == create["stdout"]
    assert verify["stdout"] == {
        "artifact": "demo/evidence/run-verification.json",
        "bytes": len(verification_bytes),
        "sha256": _sha256(verification_path),
    }
    assert verify["stderr"] == create["stdout"]


def test_provenance_source_binds_actual_schema_hashes_and_safe_inventory() -> None:
    source_path = DEMO / "evidence" / "provenance-source.json"
    source = _json_object(source_path)
    run_path = DEMO / "evidence" / "replay-run.json"
    verification_path = DEMO / "evidence" / "run-verification.json"
    manifest_path = DEMO / "replay-manifest.json"
    run = provenance.load_replay_run(run_path)
    manifest, manifest_sha = replay.load_manifest(manifest_path)
    descriptors = tuple(
        replay.descriptor_for(image) for image in file_io.load_images(DEMO / "inputs")
    )
    provenance.verify_replay_run(
        run=run,
        manifest=manifest,
        manifest_file_sha256=manifest_sha,
        descriptors=descriptors,
    )

    assert source["schema_version"] == 1
    assert source["privacy"] == "synthetic fixtures only; no provider request"
    assert source["environment"] == {
        "OPENAI_API_KEY": "absent",
        "PYTHONPATH_prefix": "demo/failures/provider-sentinel:src",
    }
    assert len(run_path.read_bytes()) == 1038
    assert _sha256(run_path) == (
        "sha256:652a25624f6496a28a27752a55c0582736c55908777e53cb0fd151a935bdfee7"
    )
    assert len(verification_path.read_bytes()) == 70
    assert _sha256(verification_path) == (
        "sha256:14a4cc7357c4031b9ae61da38c88d35d1ceed5e5442db082bb58f3b7e5d62484"
    )

    bindings = cast(list[dict[str, str]], source["bindings"])
    assert [binding["id"] for binding in bindings] == [
        "input_batch",
        "receipt_contract",
        "replay_manifest_file",
        "run_id",
    ]
    assert [binding["value"] for binding in bindings] == [
        manifest.batch.digest,
        provenance.receipt_contract_digest(),
        _sha256(manifest_path),
        provenance.run_id_for(run.body),
    ]
    assert run.body.input_batch_digest == bindings[0]["value"]
    assert run.body.contract.digest == bindings[1]["value"]
    assert run.body.replay_manifest_file_sha256 == bindings[2]["value"]
    assert run.run_id == bindings[3]["value"]
    edges = cast(list[dict[str, str]], source["verifier_edges"])
    assert [edge["binding"] for edge in edges] == [
        binding["id"] for binding in bindings
    ]
    checks = cast(list[str], source["verification_checks"])
    assert checks == [
        "bounded pinned JSON reads",
        "normal image preflight",
        "exact ordered input names",
        "strict typed output equality",
        "current receipt schema",
    ]

    artifacts = cast(list[dict[str, Any]], source["artifacts"])
    assert [record["path"] for record in artifacts] == sorted(
        {
            *(f"demo/{path}" for path in EXPECTED_DEMO_FILES),
            *(f"docs/assets/{path}" for path in EXPECTED_ASSET_FILES),
        }
        - {"demo/evidence/provenance-source.json"}
    )
    for record in artifacts:
        path = REPOSITORY / str(record["path"])
        assert record["bytes"] == path.stat().st_size
        assert record["sha256"] == _sha256(path)

    sources = cast(list[dict[str, Any]], source["sources"])
    assert [record["path"] for record in sources] == sorted(EXPECTED_SOURCE_FILES)
    for record in sources:
        path = REPOSITORY / str(record["path"])
        assert record["bytes"] == path.stat().st_size
        assert record["sha256"] == _sha256(path)

    evidence_text = source_path.read_text(encoding="ascii")
    for forbidden in (
        str(REPOSITORY),
        "/home/",
        "Traceback",
        "sk-",
        "ghp_",
        "github_pat_",
    ):
        assert forbidden not in evidence_text


def test_failure_fixtures_encode_real_distinct_boundary_cases() -> None:
    valid = (DEMO / "failures" / "corrupt-batch" / "01-valid.png").read_bytes()
    corrupt = (DEMO / "failures" / "corrupt-batch" / "02-corrupt.png").read_bytes()
    images = file_io.load_images(DEMO / "inputs")
    reversed_manifest, _ = replay.load_manifest(
        DEMO / "failures" / "reversed-replay-manifest.json"
    )

    assert valid == (DEMO / "inputs" / "cafe-lumen.png").read_bytes()
    assert corrupt.startswith(valid)
    assert corrupt.removeprefix(valid) == b"PK\x03\x04SYNTHETIC-TRAILING-PAYLOAD"
    assert [item.input.name for item in reversed_manifest.batch.items] == [
        image.name for image in reversed(images)
    ]
    with pytest.raises(
        file_io.ImageInputError,
        match="trailing or incomplete container data",
    ):
        file_io.load_images(DEMO / "failures" / "corrupt-batch")
    with pytest.raises(replay.ReplayError, match="does not match the input batch"):
        replay.ReplayProvider.bind(
            DEMO / "failures" / "reversed-replay-manifest.json",
            images,
        )
    assert not (DEMO / "failures" / "mismatch-must-not-exist.json").exists()
    assert _json_object(DEMO / "failures" / "existing-output.json") == {
        "schema_version": 1,
        "sentinel": "this synthetic file must never be replaced",
    }
    assert (DEMO / "failures" / "provider-sentinel" / "openai.py").read_text(
        encoding="ascii"
    ) == ('raise RuntimeError("provider boundary crossed during preflight evidence")\n')


def test_failure_evidence_reexecutes_exactly_and_preserves_existing_output(
    tmp_path: Path,
) -> None:
    evidence_path = DEMO / "evidence" / "failure-paths.json"
    evidence = _json_object(evidence_path)
    cases = cast(list[dict[str, Any]], evidence["cases"])
    source_output = DEMO / "failures" / "existing-output.json"
    source_before = hashlib.sha256(source_output.read_bytes()).hexdigest()
    scratch_demo = tmp_path / "demo"
    shutil.copytree(DEMO / "inputs", scratch_demo / "inputs")
    shutil.copytree(DEMO / "failures", scratch_demo / "failures")
    shutil.copy2(DEMO / "replay-manifest.json", scratch_demo)
    existing_output = scratch_demo / "failures" / "existing-output.json"
    before = hashlib.sha256(existing_output.read_bytes()).hexdigest()
    logical_commands = {
        "corrupt-image": (
            "demo/failures/corrupt-batch",
            "--acknowledge-remote-upload",
            "--stdout",
        ),
        "batch-file-limit": (
            "demo/inputs",
            "--max-files",
            "1",
            "--acknowledge-remote-upload",
            "--stdout",
        ),
        "replay-mismatch": (
            "demo/inputs",
            "--replay",
            "demo/failures/reversed-replay-manifest.json",
            "--output",
            "demo/failures/mismatch-must-not-exist.json",
        ),
        "no-clobber-output": (
            "demo/inputs",
            "--replay",
            "demo/replay-manifest.json",
            "--output",
            "demo/failures/existing-output.json",
        ),
    }
    commands = {
        case_id: tuple(
            str(scratch_demo / argument.removeprefix("demo/"))
            if argument.startswith("demo/")
            else argument
            for argument in arguments
        )
        for case_id, arguments in logical_commands.items()
    }

    assert evidence["schema_version"] == 1
    assert evidence["privacy"] == "synthetic fixtures only; no provider request"
    assert evidence["provider_sentinel"] == (
        "demo/failures/provider-sentinel/openai.py"
    )
    assert [case["id"] for case in cases] == list(logical_commands)
    evidence_text = evidence_path.read_text(encoding="ascii")
    for forbidden in (str(REPOSITORY), "/home/", "Traceback", "sk-", "ghp_"):
        assert forbidden not in evidence_text

    base_environment = os.environ.copy()
    base_environment.pop("OPENAI_API_KEY", None)
    base_environment.pop("PYTEST_ADDOPTS", None)
    base_environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(scratch_demo / "failures" / "provider-sentinel"),
            str(REPOSITORY / "src"),
        ]
    )
    for case in cases:
        environment = base_environment.copy()
        case_id = str(case["id"])
        live_preflight = case_id in {"corrupt-image", "batch-file-limit"}
        if live_preflight:
            environment["OPENAI_API_KEY"] = "synthetic-provider-tripwire"
        prefix = ["PYTHONPATH=demo/failures/provider-sentinel:src"]
        if live_preflight:
            prefix.insert(0, "OPENAI_API_KEY=synthetic-provider-tripwire")
        expected_reproduction = " ".join(
            [
                *prefix,
                shlex.join(
                    (
                        "python",
                        "-m",
                        "receipt_extractor.main",
                        *logical_commands[case_id],
                    )
                ),
            ]
        )
        assert case["reproduction_command"] == expected_reproduction
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "receipt_extractor.main",
                *commands[case_id],
            ],
            cwd=REPOSITORY,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == case["exit_code"]
        assert completed.stdout == case["stdout"] == ""
        assert completed.stderr == case["stderr"]

    after = hashlib.sha256(existing_output.read_bytes()).hexdigest()
    source_after = hashlib.sha256(source_output.read_bytes()).hexdigest()
    preserved = cast(dict[str, str], evidence["preserved_output"])
    mismatch_output = cast(dict[str, str | bool], evidence["replay_mismatch_output"])
    assert mismatch_output == {
        "path": "demo/failures/mismatch-must-not-exist.json",
        "exists_before": False,
        "exists_after": False,
    }
    assert preserved == {
        "path": "demo/failures/existing-output.json",
        "sha256_before": before,
        "sha256_after": after,
    }
    assert after == before
    assert source_after == source_before == before
    assert not (scratch_demo / "failures" / "mismatch-must-not-exist.json").exists()


def test_evidence_inventory_matches_exact_allowlist() -> None:
    actual_demo = {
        path.relative_to(DEMO).as_posix() for path in DEMO.rglob("*") if path.is_file()
    }
    actual_assets = {
        path.relative_to(ASSETS).as_posix()
        for path in ASSETS.rglob("*")
        if path.is_file()
    }
    assert actual_demo == EXPECTED_DEMO_FILES
    assert actual_assets == EXPECTED_ASSET_FILES

    for path in (
        *(DEMO / relative for relative in sorted(EXPECTED_DEMO_FILES)),
        *(ASSETS / relative for relative in sorted(EXPECTED_ASSET_FILES)),
    ):
        assert path.is_file(), path.relative_to(REPOSITORY)
        assert path.stat().st_size > 0, path.relative_to(REPOSITORY)


def test_raster_evidence_is_really_decodable_png_webp_and_gif() -> None:
    expected_formats = {
        DEMO / "inputs" / "cafe-lumen.png": "PNG",
        DEMO / "inputs" / "metro-line.webp": "WEBP",
        ASSETS / "cli-dry-run.png": "PNG",
        ASSETS / "cli-help.png": "PNG",
        ASSETS / "cli-provenance.png": "PNG",
        ASSETS / "cli-replay.png": "PNG",
        ASSETS / "coverage.png": "PNG",
        ASSETS / "demo-receipts.png": "PNG",
        ASSETS / "failure-boundaries.png": "PNG",
        ASSETS / "demo.gif": "GIF",
    }

    for path, expected_format in expected_formats.items():
        with Image.open(path) as image:
            assert image.format == expected_format
            assert image.width > 0
            assert image.height > 0
            frame_count = int(getattr(image, "n_frames", 1))
            for frame_index in range(frame_count):
                image.seek(frame_index)
                image.load()
            if expected_format == "GIF":
                assert frame_count == 7
            else:
                assert frame_count == 1
            assert "exif" not in image.info


def test_svg_evidence_is_parseable_inert_and_locally_referenced() -> None:
    architecture = ET.parse(ASSETS / "architecture.svg")
    coverage = ET.parse(ASSETS / "coverage.svg")
    provenance_bindings = ET.parse(ASSETS / "provenance-bindings.svg")

    for document in (architecture, coverage, provenance_bindings):
        root = document.getroot()
        assert _local_name(root.tag) == "svg"
        for element in root.iter():
            assert _local_name(element.tag) not in {"script", "foreignobject"}
            for raw_name, value in element.attrib.items():
                attribute = _local_name(raw_name)
                assert not attribute.startswith("on")
                assert "javascript:" not in value.casefold()
                if attribute == "href":
                    assert value.startswith("#")

    architecture_text = " ".join(architecture.getroot().itertext())
    for label in (
        "Receipt batch",
        "Pinned preflight",
        "Mode router",
        "Dry-run",
        "Exact replay",
        "Live Responses",
        "Typed boundary",
        "ReceiptFields",
        "Result sink",
        "Replay manifest",
        "Run builder",
        "Run verifier",
        "Fixed stdout",
        "no provider request",
    ):
        assert label in architecture_text

    source = _json_object(DEMO / "evidence" / "provenance-source.json")
    provenance_text = " ".join(provenance_bindings.getroot().itertext())
    bindings = cast(list[dict[str, str]], source["bindings"])
    for binding in bindings:
        assert binding["label"] in provenance_text
        assert binding["value"] in provenance_text
        assert binding["producer"] in provenance_text
        assert binding["run_field"] in provenance_text
    checks = cast(list[str], source["verification_checks"])
    assert "Non-hash equality checks" in provenance_text
    for check in checks:
        assert check in provenance_text
    assert "Local verifier" in provenance_text
    assert "not authenticity" in provenance_text


def test_coverage_summary_matches_svg_text_and_configured_floor() -> None:
    summary = _json_object(DEMO / "evidence" / "coverage-summary.json")
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    tool = cast(dict[str, Any], project["tool"])
    coverage_config = cast(dict[str, Any], tool["coverage"])
    report_config = cast(dict[str, Any], coverage_config["report"])
    configured_floor = float(report_config["fail_under"])

    combined_percent = float(summary["combined_percent"])
    test_count = int(summary["test_count"])
    modules = cast(list[dict[str, Any]], summary["modules"])
    coverage_text = " ".join(
        text.strip()
        for text in ET.parse(ASSETS / "coverage.svg").getroot().itertext()
        if text.strip()
    )

    assert summary["schema_version"] == 1
    assert summary["command"] == "make check"
    assert configured_floor >= 90
    assert configured_floor <= combined_percent <= 100
    assert test_count > 0
    assert f"{test_count} tests" in coverage_text
    assert f"{combined_percent:.2f}% combined" in coverage_text
    assert [module["module"] for module in modules] == [
        "artifact_io",
        "file_io",
        "main",
        "provenance",
        "replay",
        "gpt",
        "schema",
    ]
    for module in modules:
        percent = float(module["percent"])
        assert 0 <= percent <= 100
        assert str(module["module"]) in coverage_text
        assert f"{percent:.2f}%" in coverage_text

    planned_demo = subprocess.run(
        [
            "make",
            "--dry-run",
            "--no-print-directory",
            "demo",
            f"PYTHON={sys.executable}",
        ],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    bootstrap_test = planned_demo.index("--ignore=tests/test_demo_evidence.py")
    bootstrap_export = planned_demo.index(
        "-m coverage json --pretty -o .venv/demo-bootstrap-coverage.json"
    )
    bootstrap_capture = planned_demo.index(
        "--coverage-json .venv/demo-bootstrap-coverage.json"
    )
    first_full_test = planned_demo.index(
        "--cov=receipt_extractor",
        bootstrap_capture,
    )
    coverage_export = planned_demo.index(
        "-m coverage json --pretty -o .venv/demo-coverage.json"
    )
    final_capture = planned_demo.index("--coverage-json .venv/demo-coverage.json")
    final_full_test = planned_demo.index(
        "--cov=receipt_extractor",
        first_full_test + 1,
    )
    assert (
        bootstrap_test
        < bootstrap_export
        < bootstrap_capture
        < first_full_test
        < coverage_export
        < final_capture
        < final_full_test
    )
    assert planned_demo.count("--ignore=tests/test_demo_evidence.py") == 1
    assert planned_demo.count("--cov=receipt_extractor") == 3
    assert planned_demo.count("PYTEST_ADDOPTS=") == 3
    assert planned_demo.count(f"{sys.executable} -m pytest") == 3
    assert planned_demo.count(f"{sys.executable} -m coverage json") == 2


def test_readme_links_every_reader_facing_visual_and_source_evidence() -> None:
    readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
    for path in (
        "docs/assets/demo.gif",
        "docs/assets/demo-receipts.png",
        "docs/assets/architecture.svg",
        "docs/assets/cli-help.png",
        "docs/assets/cli-provenance.png",
        "docs/assets/coverage.svg",
        "docs/assets/cli-dry-run.png",
        "docs/assets/cli-replay.png",
        "docs/assets/failure-boundaries.png",
        "docs/assets/provenance-bindings.svg",
        "demo/inputs",
        "demo/replay-manifest.json",
        "demo/failures",
        "demo/evidence/help.txt",
        "demo/evidence/dry-run.json",
        "demo/evidence/failure-paths.json",
        "demo/evidence/provenance-source.json",
        "demo/evidence/replay-result.json",
        "demo/evidence/replay-run.json",
        "demo/evidence/run-verification.json",
        "demo/evidence/coverage-summary.json",
        "docs/run-provenance.md",
        "scripts/capture_demo.py",
    ):
        assert f"]({path})" in readme


def test_source_distribution_manifest_keeps_evidence_reproducible() -> None:
    directives = set(
        (REPOSITORY / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    )

    assert {
        "include Makefile",
        "include requirements.txt",
        "include requirements-dev.txt",
        "include docs/run-provenance.md",
        "recursive-include demo *",
        "recursive-include docs/assets *",
        "recursive-include scripts *.py",
        "recursive-include tests *.py",
    } <= directives
