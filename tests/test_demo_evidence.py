from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from receipt_extractor import file_io, replay

REPOSITORY = Path(__file__).resolve().parents[1]
DEMO = REPOSITORY / "demo"
ASSETS = REPOSITORY / "docs" / "assets"


def _json_object(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return cast(dict[str, Any], decoded)


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


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


def test_expected_evidence_files_exist_and_are_nonempty() -> None:
    expected = (
        DEMO / "evidence" / "coverage-summary.json",
        DEMO / "evidence" / "dry-run.json",
        DEMO / "evidence" / "failure-paths.json",
        DEMO / "evidence" / "help.txt",
        DEMO / "evidence" / "replay-result.json",
        DEMO / "failures" / "corrupt-batch" / "01-valid.png",
        DEMO / "failures" / "corrupt-batch" / "02-corrupt.png",
        DEMO / "failures" / "existing-output.json",
        DEMO / "failures" / "provider-sentinel" / "openai.py",
        DEMO / "failures" / "reversed-replay-manifest.json",
        DEMO / "inputs" / "cafe-lumen.png",
        DEMO / "inputs" / "metro-line.webp",
        DEMO / "replay-manifest.json",
        ASSETS / "architecture.svg",
        ASSETS / "cli-dry-run.png",
        ASSETS / "cli-help.png",
        ASSETS / "cli-replay.png",
        ASSETS / "coverage.png",
        ASSETS / "coverage.svg",
        ASSETS / "demo-receipts.png",
        ASSETS / "demo.gif",
        ASSETS / "failure-boundaries.png",
    )

    for path in expected:
        assert path.is_file(), path.relative_to(REPOSITORY)
        assert path.stat().st_size > 0, path.relative_to(REPOSITORY)


def test_raster_evidence_is_really_decodable_png_webp_and_gif() -> None:
    expected_formats = {
        DEMO / "inputs" / "cafe-lumen.png": "PNG",
        DEMO / "inputs" / "metro-line.webp": "WEBP",
        ASSETS / "cli-dry-run.png": "PNG",
        ASSETS / "cli-help.png": "PNG",
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
                assert frame_count == 6
            else:
                assert frame_count == 1
            assert "exif" not in image.info


def test_svg_evidence_is_parseable_inert_and_locally_referenced() -> None:
    architecture = ET.parse(ASSETS / "architecture.svg")
    coverage = ET.parse(ASSETS / "coverage.svg")

    for document in (architecture, coverage):
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
        "Dry-run",
        "Exact replay",
        "Live Responses",
        "Typed boundary",
        "ReceiptFields",
        "Private sink",
        "Replay manifest",
        "Offline: no key, no OpenAI import",
    ):
        assert label in architecture_text


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
        "file_io",
        "main",
        "replay",
        "gpt",
        "schema",
    ]
    for module in modules:
        percent = float(module["percent"])
        assert 0 <= percent <= 100
        assert str(module["module"]) in coverage_text
        assert f"{percent:.2f}%" in coverage_text


def test_readme_links_every_reader_facing_visual_and_source_evidence() -> None:
    readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
    for path in (
        "docs/assets/demo.gif",
        "docs/assets/demo-receipts.png",
        "docs/assets/architecture.svg",
        "docs/assets/cli-help.png",
        "docs/assets/coverage.svg",
        "docs/assets/cli-dry-run.png",
        "docs/assets/cli-replay.png",
        "docs/assets/failure-boundaries.png",
        "demo/inputs",
        "demo/replay-manifest.json",
        "demo/failures",
        "demo/evidence/help.txt",
        "demo/evidence/dry-run.json",
        "demo/evidence/failure-paths.json",
        "demo/evidence/replay-result.json",
        "demo/evidence/coverage-summary.json",
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
        "recursive-include demo *",
        "recursive-include docs/assets *",
        "recursive-include scripts *.py",
        "recursive-include tests *.py",
    } <= directives
