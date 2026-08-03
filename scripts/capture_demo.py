"""Generate deterministic synthetic demo inputs and real CLI evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from PIL import Image, ImageDraw, ImageFont, ImageOps

from receipt_extractor import evaluation, file_io, replay
from receipt_extractor.schema import ExpenseCategory, ReceiptFields

CANVAS = "#0b1020"
PANEL = "#121a2e"
PANEL_LIGHT = "#19243d"
TEXT = "#edf2ff"
MUTED = "#9aa8c7"
CYAN = "#57d3ff"
GREEN = "#63e6a5"
AMBER = "#ffc857"
RED = "#ff7b8b"
PURPLE = "#a78bfa"
RECEIPT = "#fffdf7"
INK = "#172033"
RECEIPT_MUTED = "#657087"
RECEIPT_ALERT = "#92243a"

EXPECTED_DEMO_FILES = (
    "demo/evidence/coverage-summary.json",
    "demo/evidence/dry-run.json",
    "demo/evidence/evaluation-receipt.json",
    "demo/evidence/evaluation-receipt.txt",
    "demo/evidence/evaluation-verification.json",
    "demo/evidence/failure-paths.json",
    "demo/evidence/help.txt",
    "demo/evidence/provenance-source.json",
    "demo/evidence/replay-result.json",
    "demo/evidence/replay-run.json",
    "demo/evidence/run-verification.json",
    "demo/failures/corrupt-batch/01-valid.png",
    "demo/failures/corrupt-batch/02-corrupt.png",
    "demo/failures/existing-output.json",
    "demo/failures/provider-sentinel/openai.py",
    "demo/failures/reversed-replay-manifest.json",
    "demo/evaluation-inputs/01-exact-cafe.png",
    "demo/evaluation-inputs/02-metro-amount-category.png",
    "demo/evaluation-inputs/03-hotel-date-vendor.png",
    "demo/evaluation-inputs/04-office-date-amount.png",
    "demo/evaluation-inputs/05-cinema-vendor-category.png",
    "demo/evaluation-inputs/06-exact-kiosk.png",
    "demo/evaluation-inputs/07-null-date-vendor.png",
    "demo/evaluation-inputs/08-null-amount-category.png",
    "demo/evaluation-suite.json",
    "demo/inputs/cafe-lumen.png",
    "demo/inputs/metro-line.webp",
    "demo/replay-manifest.json",
)
EXPECTED_ASSET_FILES = (
    "docs/assets/architecture.svg",
    "docs/assets/cli-dry-run.png",
    "docs/assets/cli-help.png",
    "docs/assets/cli-provenance.png",
    "docs/assets/cli-replay.png",
    "docs/assets/coverage.png",
    "docs/assets/coverage.svg",
    "docs/assets/demo-receipts.png",
    "docs/assets/demo.gif",
    "docs/assets/evaluation-bindings.svg",
    "docs/assets/evaluation-confusion.svg",
    "docs/assets/evaluation-fixtures.png",
    "docs/assets/evaluation-scorecard.svg",
    "docs/assets/cli-evaluation.png",
    "docs/assets/failure-boundaries.png",
    "docs/assets/provenance-bindings.svg",
)
EXPECTED_GENERATED_FILES = tuple(sorted((*EXPECTED_DEMO_FILES, *EXPECTED_ASSET_FILES)))
PROVENANCE_SOURCE_FILES = tuple(
    sorted(
        (
            "MANIFEST.in",
            "Makefile",
            "README.md",
            "docs/run-provenance.md",
            "docs/synthetic-evaluation.md",
            "pyproject.toml",
            "requirements-dev.txt",
            "requirements.txt",
            "scripts/capture_demo.py",
            "src/receipt_extractor/__init__.py",
            "src/receipt_extractor/artifact_io.py",
            "src/receipt_extractor/file_io.py",
            "src/receipt_extractor/evaluation.py",
            "src/receipt_extractor/evaluation_cli.py",
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
            "tests/test_evaluation_cli.py",
            "tests/test_evaluation_io.py",
            "tests/test_evaluation_report.py",
            "tests/test_evaluation_suite.py",
            "tests/test_gpt.py",
            "tests/test_main_internals.py",
            "tests/test_provenance.py",
            "tests/test_replay.py",
            "tests/test_schema.py",
        )
    )
)


@dataclass(frozen=True, slots=True)
class _CompletedCommand:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class _FailureSpec:
    id: str
    title: str
    logical_arguments: tuple[str, ...]
    exit_code: int
    stderr: str
    invariant: str
    arm_provider_tripwire: bool = False


class _FailureCase(TypedDict):
    id: str
    title: str
    reproduction_command: str
    exit_code: int
    stdout: str
    stderr: str
    invariant: str


class _PreservedOutput(TypedDict):
    path: str
    sha256_before: str
    sha256_after: str


class _AbsentOutput(TypedDict):
    path: str
    exists_before: bool
    exists_after: bool


class _FailureEvidence(TypedDict):
    schema_version: int
    privacy: str
    provider_sentinel: str
    cases: list[_FailureCase]
    replay_mismatch_output: _AbsentOutput
    preserved_output: _PreservedOutput


class _ProvenanceCapture(TypedDict):
    run_output: str
    verification_output: str
    source: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _EvaluationFixtureSpec:
    case_id: str
    filename: str
    truth: ReceiptFields
    candidate: ReceiptFields


@dataclass(frozen=True, slots=True)
class _EvaluationCapture:
    suite: evaluation.EvaluationSuite
    report: evaluation.EvaluationReport
    json_output: str
    text_output: str
    verification_output: str


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Use Pillow's pinned bundled font instead of a host font."""
    return ImageFont.load_default(size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, *, size: int) -> int:
    box = draw.textbbox((0, 0), text, font=_font(size))
    return round(box[2] - box[0])


def _centered(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    *,
    width: int,
    size: int,
    fill: str,
) -> None:
    x = (width - _text_width(draw, text, size=size)) // 2
    draw.text((x, y), text, font=_font(size), fill=fill)


def _receipt(
    *,
    vendor: str,
    subtitle: str,
    lines: Sequence[tuple[str, str]],
    subtotal: str,
    tax: str,
    total: str,
) -> Image.Image:
    width, height = 720, 1080
    image = Image.new("RGB", (width, height), RECEIPT)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (18, 18, width - 18, height - 18),
        radius=26,
        outline="#d5d9e2",
        width=3,
    )
    _centered(
        draw,
        68,
        "SYNTHETIC - NOT A REAL RECEIPT",
        width=width,
        size=19,
        fill=RED,
    )
    _centered(draw, 126, vendor.upper(), width=width, size=44, fill=INK)
    _centered(draw, 188, subtitle, width=width, size=22, fill=RECEIPT_MUTED)
    _centered(
        draw,
        238,
        "2026-07-24  12:40 UTC",
        width=width,
        size=19,
        fill=RECEIPT_MUTED,
    )
    draw.line((72, 300, width - 72, 300), fill="#cfd5df", width=3)

    y = 342
    for label, price in lines:
        draw.text((82, y), label, font=_font(25), fill=INK)
        price_width = _text_width(draw, price, size=25)
        draw.text((width - 82 - price_width, y), price, font=_font(25), fill=INK)
        y += 68

    draw.line((72, 650, width - 72, 650), fill="#cfd5df", width=3)
    totals = (("SUBTOTAL", subtotal), ("TAX", tax))
    y = 694
    for label, value in totals:
        draw.text((82, y), label, font=_font(23), fill=RECEIPT_MUTED)
        value_width = _text_width(draw, value, size=23)
        draw.text(
            (width - 82 - value_width, y),
            value,
            font=_font(23),
            fill=RECEIPT_MUTED,
        )
        y += 58

    draw.rounded_rectangle(
        (66, 822, width - 66, 916),
        radius=18,
        fill="#eaf0ff",
        outline="#b8c7ef",
        width=2,
    )
    draw.text((88, 848), "TOTAL", font=_font(30), fill=INK)
    total_width = _text_width(draw, total, size=34)
    draw.text(
        (width - 88 - total_width, 843),
        total,
        font=_font(34),
        fill=INK,
    )
    _centered(
        draw,
        970,
        "Fixture generated by scripts/capture_demo.py",
        width=width,
        size=18,
        fill=RECEIPT_MUTED,
    )
    return image


def _save_inputs(input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    cafe = _receipt(
        vendor="Cafe Lumen",
        subtitle="Fictional demo vendor",
        lines=(("Oat latte", "$6.40"), ("Lunch bowl", "$10.20")),
        subtotal="$16.60",
        tax="$1.80",
        total="$18.40",
    )
    metro = _receipt(
        vendor="Metro Line",
        subtitle="Fictional demo transit",
        lines=(("Single ride", "$3.00"),),
        subtotal="$3.00",
        tax="$0.25",
        total="$3.25",
    )
    cafe.save(input_dir / "cafe-lumen.png", format="PNG", compress_level=9)
    metro.save(
        input_dir / "metro-line.webp",
        format="WEBP",
        lossless=True,
        quality=100,
        method=6,
    )


def _fields(
    date: str | None,
    amount: str | None,
    vendor: str | None,
    category: ExpenseCategory | None,
) -> ReceiptFields:
    return ReceiptFields(
        date=date,
        amount=amount,
        vendor=vendor,
        category=category,
    )


def _evaluation_fixture_specs() -> tuple[_EvaluationFixtureSpec, ...]:
    return (
        _EvaluationFixtureSpec(
            case_id="exact-cafe",
            filename="01-exact-cafe.png",
            truth=_fields(
                "2026-07-24",
                "$18.40",
                "Cafe Lumen",
                ExpenseCategory.MEALS,
            ),
            candidate=_fields(
                "2026-07-24",
                "$18.40",
                "Cafe Lumen",
                ExpenseCategory.MEALS,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="metro-amount-category",
            filename="02-metro-amount-category.png",
            truth=_fields(
                "2026-07-25",
                "$3.25",
                "Metro Line",
                ExpenseCategory.TRANSPORT,
            ),
            candidate=_fields(
                "2026-07-25",
                "$3.52",
                "Metro Line",
                None,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="hotel-date-vendor",
            filename="03-hotel-date-vendor.png",
            truth=_fields(
                "2026-07-26",
                "$142.00",
                "Northstar Hotel",
                ExpenseCategory.LODGING,
            ),
            candidate=_fields(
                None,
                "$142.00",
                "North Star Hotel",
                ExpenseCategory.LODGING,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="office-date-amount",
            filename="04-office-date-amount.png",
            truth=_fields(
                "2026-07-27",
                "$27.80",
                "Paper Square",
                ExpenseCategory.OFFICE_SUPPLIES,
            ),
            candidate=_fields(
                "2026-07-17",
                None,
                "Paper Square",
                ExpenseCategory.OFFICE_SUPPLIES,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="cinema-vendor-category",
            filename="05-cinema-vendor-category.png",
            truth=_fields(
                "2026-07-28",
                "$14.00",
                "Orbit Cinema",
                ExpenseCategory.ENTERTAINMENT,
            ),
            candidate=_fields(
                "2026-07-28",
                "$14.00",
                None,
                ExpenseCategory.MEALS,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="exact-kiosk",
            filename="06-exact-kiosk.png",
            truth=_fields(
                "2026-07-29",
                "$9.90",
                "Civic Kiosk",
                ExpenseCategory.OTHER,
            ),
            candidate=_fields(
                "2026-07-29",
                "$9.90",
                "Civic Kiosk",
                ExpenseCategory.OTHER,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="null-date-vendor",
            filename="07-null-date-vendor.png",
            truth=_fields(None, "$6.75", None, ExpenseCategory.OTHER),
            candidate=_fields(
                "2026-07-30",
                "$6.75",
                "Unnamed Stall",
                ExpenseCategory.OTHER,
            ),
        ),
        _EvaluationFixtureSpec(
            case_id="null-amount-category",
            filename="08-null-amount-category.png",
            truth=_fields("2026-07-31", None, "Pop-up Booth", None),
            candidate=_fields(
                "2026-07-31",
                "$12.00",
                "Pop-up Booth",
                ExpenseCategory.OTHER,
            ),
        ),
    )


def _evaluation_value(value: object) -> str:
    if value is None:
        return "<NOT PRINTED>"
    if isinstance(value, ExpenseCategory):
        return value.value
    return str(value)


def _evaluation_receipt(spec: _EvaluationFixtureSpec) -> Image.Image:
    width, height = 720, 900
    image = Image.new("RGB", (width, height), RECEIPT)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (18, 18, width - 18, height - 18),
        radius=26,
        outline="#d5d9e2",
        width=3,
    )
    _centered(
        draw,
        58,
        "SYNTHETIC EVALUATION FIXTURE",
        width=width,
        size=22,
        fill=RECEIPT_ALERT,
    )
    _centered(draw, 112, spec.case_id, width=width, size=34, fill=INK)
    _centered(
        draw,
        166,
        "Repository-authored truth; never a model output",
        width=width,
        size=18,
        fill=RECEIPT_MUTED,
    )
    draw.line((66, 222, width - 66, 222), fill="#cfd5df", width=3)
    draw.text((72, 264), "AUTHORED TRUTH", font=_font(24), fill=INK)

    rows = (
        ("DATE", spec.truth.date),
        ("AMOUNT", spec.truth.amount),
        ("VENDOR", spec.truth.vendor),
        ("CATEGORY", spec.truth.category),
    )
    y = 330
    for label, raw_value in rows:
        value = _evaluation_value(raw_value)
        draw.rounded_rectangle(
            (66, y - 14, width - 66, y + 76),
            radius=16,
            fill="#f3f6fb",
            outline="#d6dde9",
            width=2,
        )
        draw.text((88, y + 9), label, font=_font(19), fill=RECEIPT_MUTED)
        value_width = _text_width(draw, value, size=23)
        draw.text(
            (width - 88 - value_width, y + 6),
            value,
            font=_font(23),
            fill=RECEIPT_ALERT if raw_value is None else INK,
        )
        y += 112

    _centered(
        draw,
        800,
        "Candidate values live only in evaluation-suite.json",
        width=width,
        size=18,
        fill=RECEIPT_MUTED,
    )
    _centered(
        draw,
        835,
        "Generated deterministically by scripts/capture_demo.py",
        width=width,
        size=17,
        fill=RECEIPT_MUTED,
    )
    return image


def _save_evaluation_inputs(
    input_dir: Path,
    specs: Sequence[_EvaluationFixtureSpec],
) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        _evaluation_receipt(spec).save(
            input_dir / spec.filename,
            format="PNG",
            compress_level=9,
        )


def _manifest_document(images: Sequence[file_io.ImagePayload]) -> dict[str, Any]:
    outputs = {
        "cafe-lumen.png": {
            "date": "2026-07-24",
            "amount": "$18.40",
            "vendor": "Cafe Lumen",
            "category": "Meals",
        },
        "metro-line.webp": {
            "date": "2026-07-24",
            "amount": "$3.25",
            "vendor": "Metro Line",
            "category": "Transport",
        },
    }
    descriptors = [replay.descriptor_for(image) for image in images]
    return {
        "kind": replay.REPLAY_KIND,
        "schema_version": 1,
        "batch": {
            "digest": replay.batch_digest(descriptors),
            "items": [
                {
                    "input": descriptor.model_dump(mode="json"),
                    "output": outputs[descriptor.name],
                }
                for descriptor in descriptors
            ],
        },
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    path.write_text(
        f"{serialized}\n",
        encoding="ascii",
    )


def _execute(
    repository: Path,
    arguments: Sequence[str],
    *,
    pythonpath_prefixes: Sequence[Path] = (),
    arm_provider_tripwire: bool = False,
) -> _CompletedCommand:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    if arm_provider_tripwire:
        environment["OPENAI_API_KEY"] = "synthetic-provider-tripwire"
    environment["COLUMNS"] = "96"
    environment["LINES"] = "30"
    environment["LC_ALL"] = "C.UTF-8"
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            *(str(path) for path in pythonpath_prefixes),
            str(repository / "src"),
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-m", "receipt_extractor.main", *arguments],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return _CompletedCommand(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run(
    repository: Path,
    arguments: Sequence[str],
) -> str:
    completed = _execute(repository, arguments)
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError(
            f"demo command failed with status {completed.returncode}; "
            "stderr intentionally suppressed"
        )
    return completed.stdout


def _execute_evaluator(
    repository: Path,
    arguments: Sequence[str],
    *,
    sentinel: Path,
) -> _CompletedCommand:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    environment["COLUMNS"] = "96"
    environment["LINES"] = "30"
    environment["LC_ALL"] = "C.UTF-8"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(sentinel), str(repository / "src"))
    )
    completed = subprocess.run(
        [sys.executable, "-m", "receipt_extractor.evaluation_cli", *arguments],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return _CompletedCommand(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _successful_evaluator_command(completed: _CompletedCommand) -> str:
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError(
            f"evaluation evidence command failed with status "
            f"{completed.returncode}; stderr intentionally suppressed"
        )
    return completed.stdout


def _capture_evaluation_evidence(
    *,
    repository: Path,
    demo_dir: Path,
    input_dir: Path,
    evidence_dir: Path,
    specs: Sequence[_EvaluationFixtureSpec],
) -> _EvaluationCapture:
    images = file_io.load_images(input_dir)
    image_by_name = {image.name: image for image in images}
    expected_names = [spec.filename for spec in specs]
    if [image.name for image in images] != expected_names:
        raise RuntimeError("evaluation input order does not match fixture order")

    cases = tuple(
        evaluation.EvaluationCase(
            case_id=spec.case_id,
            input=replay.descriptor_for(image_by_name[spec.filename]),
            truth=spec.truth,
            candidate=spec.candidate,
        )
        for spec in specs
    )
    suite = evaluation.build_evaluation_suite(
        name="balanced-authored-negative-control-v1",
        cases=cases,
    )
    suite_path = demo_dir / "evaluation-suite.json"
    suite_path.write_text(
        evaluation.evaluation_suite_json(suite),
        encoding="ascii",
    )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / "evaluation-receipt.json"
    text_path = evidence_dir / "evaluation-receipt.txt"
    verification_path = evidence_dir / "evaluation-verification.json"
    scratch_parent = repository / ".venv" / "evidence-scratch"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="evaluation-boundary-",
        dir=scratch_parent,
    ) as temporary:
        sentinel = Path(temporary)
        (sentinel / "openai.py").write_text(
            'raise RuntimeError("provider import crossed during evaluation")\n',
            encoding="ascii",
        )
        (sentinel / "sitecustomize.py").write_text(
            "import socket\n"
            "def _blocked(*_args, **_kwargs):\n"
            "    raise RuntimeError('network crossed during evaluation')\n"
            "socket.socket = _blocked\n"
            "socket.create_connection = _blocked\n",
            encoding="ascii",
        )

        json_output = _successful_evaluator_command(
            _execute_evaluator(
                repository,
                ("evaluate", str(suite_path)),
                sentinel=sentinel,
            )
        )
        text_output = _successful_evaluator_command(
            _execute_evaluator(
                repository,
                ("evaluate", str(suite_path), "--format", "text"),
                sentinel=sentinel,
            )
        )
        report_path.write_text(json_output, encoding="ascii")
        report = evaluation.load_evaluation_report(report_path)
        loaded_suite = evaluation.load_evaluation_suite(suite_path)
        evaluation.verify_evaluation_report(suite=loaded_suite, report=report)
        if loaded_suite != suite:
            raise RuntimeError("evaluation suite changed during persisted reload")
        if json_output != evaluation.evaluation_report_json(report):
            raise RuntimeError("evaluation JSON presentation drifted")
        if text_output != evaluation.evaluation_report_text(report):
            raise RuntimeError("evaluation text presentation drifted")

        verification_output = _successful_evaluator_command(
            _execute_evaluator(
                repository,
                ("verify", str(suite_path), str(report_path)),
                sentinel=sentinel,
            )
        )

    text_path.write_text(text_output, encoding="ascii")
    verification_path.write_text(verification_output, encoding="ascii")
    return _EvaluationCapture(
        suite=loaded_suite,
        report=report,
        json_output=json_output,
        text_output=text_output,
        verification_output=verification_output,
    )


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _file_record(path: Path, *, logical_path: str) -> dict[str, object]:
    return {
        "path": logical_path,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _stream_record(
    value: str,
    *,
    artifact: str | None,
) -> dict[str, object]:
    encoded = value.encode("utf-8")
    return {
        "artifact": artifact,
        "bytes": len(encoded),
        "sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }


def _actual_arguments(
    demo_dir: Path,
    logical_arguments: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        str(demo_dir / argument.removeprefix("demo/"))
        if argument.startswith("demo/")
        else argument
        for argument in logical_arguments
    )


def _reproduction_command(spec: _FailureSpec) -> str:
    environment = ["PYTHONPATH=demo/failures/provider-sentinel:src"]
    if spec.arm_provider_tripwire:
        environment.insert(0, "OPENAI_API_KEY=synthetic-provider-tripwire")
    invocation = shlex.join(
        ("python", "-m", "receipt_extractor.main", *spec.logical_arguments)
    )
    return " ".join((*environment, invocation))


def _capture_failure_evidence(
    *,
    repository: Path,
    demo_dir: Path,
    input_dir: Path,
    images: Sequence[file_io.ImagePayload],
) -> _FailureEvidence:
    failure_dir = demo_dir / "failures"
    corrupt_dir = failure_dir / "corrupt-batch"
    provider_sentinel = failure_dir / "provider-sentinel"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    provider_sentinel.mkdir(parents=True, exist_ok=True)

    valid_png = (input_dir / "cafe-lumen.png").read_bytes()
    (corrupt_dir / "01-valid.png").write_bytes(valid_png)
    (corrupt_dir / "02-corrupt.png").write_bytes(
        valid_png + b"PK\x03\x04SYNTHETIC-TRAILING-PAYLOAD"
    )
    (provider_sentinel / "openai.py").write_text(
        'raise RuntimeError("provider boundary crossed during preflight evidence")\n',
        encoding="ascii",
    )

    reversed_manifest = failure_dir / "reversed-replay-manifest.json"
    _write_json(reversed_manifest, _manifest_document(list(reversed(images))))
    existing_output = failure_dir / "existing-output.json"
    _write_json(
        existing_output,
        {
            "schema_version": 1,
            "sentinel": "this synthetic file must never be replaced",
        },
    )
    tracked_preserved_sha = hashlib.sha256(existing_output.read_bytes()).hexdigest()
    tracked_mismatch_output = failure_dir / "mismatch-must-not-exist.json"
    if tracked_mismatch_output.exists():
        raise RuntimeError("the replay-mismatch output path must start absent")

    specs = (
        _FailureSpec(
            id="corrupt-image",
            title="Trailing payload",
            logical_arguments=(
                "demo/failures/corrupt-batch",
                "--acknowledge-remote-upload",
                "--stdout",
            ),
            exit_code=2,
            stderr=(
                "input validation failed: an input image has trailing or "
                "incomplete container data\n"
            ),
            invariant=(
                "A valid first member plus one appended-payload PNG rejects "
                "the whole live batch before provider import."
            ),
            arm_provider_tripwire=True,
        ),
        _FailureSpec(
            id="batch-file-limit",
            title="Batch limit",
            logical_arguments=(
                "demo/inputs",
                "--max-files",
                "1",
                "--acknowledge-remote-upload",
                "--stdout",
            ),
            exit_code=2,
            stderr=(
                "input validation failed: input images exceed the file-count limit\n"
            ),
            invariant=(
                "The live path returns no partial prefix when the complete "
                "batch exceeds its cap."
            ),
            arm_provider_tripwire=True,
        ),
        _FailureSpec(
            id="replay-mismatch",
            title="Replay mismatch",
            logical_arguments=(
                "demo/inputs",
                "--replay",
                "demo/failures/reversed-replay-manifest.json",
                "--output",
                "demo/failures/mismatch-must-not-exist.json",
            ),
            exit_code=2,
            stderr=(
                "replay validation failed; details are suppressed to avoid "
                "leaking receipt data\n"
            ),
            invariant="A valid manifest for the wrong order is rejected and redacted.",
        ),
        _FailureSpec(
            id="no-clobber-output",
            title="No-clobber sink",
            logical_arguments=(
                "demo/inputs",
                "--replay",
                "demo/replay-manifest.json",
                "--output",
                "demo/failures/existing-output.json",
            ),
            exit_code=1,
            stderr=(
                "output failed: the output path already exists; "
                "refusing to replace it\n"
            ),
            invariant="The existing sink remains byte-for-byte unchanged.",
        ),
    )

    scratch_parent = repository / ".venv" / "evidence-scratch"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    cases: list[_FailureCase] = []
    with tempfile.TemporaryDirectory(
        prefix="receipt-failures-",
        dir=scratch_parent,
    ) as temporary:
        scratch_demo = Path(temporary) / "demo"
        shutil.copytree(input_dir, scratch_demo / "inputs")
        shutil.copytree(failure_dir, scratch_demo / "failures")
        shutil.copy2(demo_dir / "replay-manifest.json", scratch_demo)
        scratch_provider = scratch_demo / "failures" / "provider-sentinel"
        scratch_existing = scratch_demo / "failures" / "existing-output.json"
        mismatch_output = scratch_demo / "failures" / "mismatch-must-not-exist.json"
        preserved_before = hashlib.sha256(scratch_existing.read_bytes()).hexdigest()
        mismatch_existed_before = mismatch_output.exists()
        if preserved_before != tracked_preserved_sha or mismatch_existed_before:
            raise RuntimeError("failure evidence scratch inputs do not match fixtures")

        for spec in specs:
            completed = _execute(
                repository,
                _actual_arguments(scratch_demo, spec.logical_arguments),
                pythonpath_prefixes=(scratch_provider,),
                arm_provider_tripwire=spec.arm_provider_tripwire,
            )
            if (
                completed.returncode != spec.exit_code
                or completed.stdout
                or completed.stderr != spec.stderr
            ):
                raise RuntimeError(
                    f"failure evidence drifted for {spec.id}; "
                    "captured details intentionally suppressed"
                )
            cases.append(
                {
                    "id": spec.id,
                    "title": spec.title,
                    "reproduction_command": _reproduction_command(spec),
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "invariant": spec.invariant,
                }
            )

        preserved_after = hashlib.sha256(scratch_existing.read_bytes()).hexdigest()
        mismatch_exists_after = mismatch_output.exists()
        if preserved_after != preserved_before:
            raise RuntimeError(
                "the no-clobber failure case changed its existing output"
            )
        if mismatch_exists_after:
            raise RuntimeError("replay mismatch reserved an output before rejecting")

    return {
        "schema_version": 1,
        "privacy": "synthetic fixtures only; no provider request",
        "provider_sentinel": "demo/failures/provider-sentinel/openai.py",
        "cases": cases,
        "replay_mismatch_output": {
            "path": "demo/failures/mismatch-must-not-exist.json",
            "exists_before": mismatch_existed_before,
            "exists_after": mismatch_exists_after,
        },
        "preserved_output": {
            "path": "demo/failures/existing-output.json",
            "sha256_before": preserved_before,
            "sha256_after": preserved_after,
        },
    }


def _provenance_command(arguments: Sequence[str]) -> str:
    invocation = shlex.join(("python", "-m", "receipt_extractor.main", *arguments))
    return f"PYTHONPATH=demo/failures/provider-sentinel:src {invocation}"


def _capture_provenance_evidence(
    *,
    repository: Path,
    demo_dir: Path,
    evidence_dir: Path,
) -> _ProvenanceCapture:
    logical_create = (
        "demo/inputs",
        "--replay",
        "demo/replay-manifest.json",
        "--run-output",
        "demo/evidence/replay-run.json",
    )
    logical_verify = (
        "demo/inputs",
        "--verify-run",
        "demo/evidence/replay-run.json",
        "--against-manifest",
        "demo/replay-manifest.json",
    )
    scratch_parent = repository / ".venv" / "evidence-scratch"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="receipt-provenance-",
        dir=scratch_parent,
    ) as temporary:
        scratch_root = Path(temporary)
        scratch_root.chmod(0o700)
        scratch_demo = scratch_root / "demo"
        scratch_evidence = scratch_demo / "evidence"
        scratch_failures = scratch_demo / "failures"
        scratch_evidence.mkdir(parents=True, mode=0o700)
        shutil.copytree(demo_dir / "inputs", scratch_demo / "inputs")
        shutil.copytree(
            demo_dir / "failures" / "provider-sentinel",
            scratch_failures / "provider-sentinel",
        )
        shutil.copy2(
            demo_dir / "replay-manifest.json",
            scratch_demo / "replay-manifest.json",
        )
        provider_sentinel = scratch_failures / "provider-sentinel"

        create = _execute(
            repository,
            _actual_arguments(scratch_demo, logical_create),
            pythonpath_prefixes=(provider_sentinel,),
        )
        if create.returncode != 0 or create.stdout or create.stderr:
            raise RuntimeError(
                "provenance creation evidence drifted; "
                "captured details intentionally suppressed"
            )
        scratch_run = scratch_evidence / "replay-run.json"
        if not scratch_run.is_file():
            raise RuntimeError("provenance creation did not publish its run bundle")
        run_details = scratch_run.lstat()
        if (
            not stat.S_ISREG(run_details.st_mode)
            or run_details.st_nlink != 1
            or stat.S_IMODE(run_details.st_mode) != 0o600
            or run_details.st_size < 1
        ):
            raise RuntimeError(
                "provenance creation did not publish one nonempty private file"
            )
        run_bytes = scratch_run.read_bytes()

        verify = _execute(
            repository,
            _actual_arguments(scratch_demo, logical_verify),
            pythonpath_prefixes=(provider_sentinel,),
        )
        expected_verification = (
            '{\n  "mode": "verify-run",\n  "schema_version": 1,\n'
            '  "verified": true\n}\n'
        )
        if (
            verify.returncode != 0
            or verify.stderr
            or verify.stdout != expected_verification
        ):
            raise RuntimeError(
                "provenance verification evidence drifted; "
                "captured details intentionally suppressed"
            )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        tracked_run = evidence_dir / "replay-run.json"
        tracked_run.write_bytes(run_bytes)
        verification_path = evidence_dir / "run-verification.json"
        verification_path.write_text(verify.stdout, encoding="ascii")

    run_document = json.loads(run_bytes)
    body = run_document["body"]
    contract = body["contract"]
    source: dict[str, Any] = {
        "schema_version": 1,
        "privacy": "synthetic fixtures only; no provider request",
        "environment": {
            "OPENAI_API_KEY": "absent",
            "PYTHONPATH_prefix": "demo/failures/provider-sentinel:src",
        },
        "commands": {
            "create": {
                "argv": [
                    "python",
                    "-m",
                    "receipt_extractor.main",
                    *logical_create,
                ],
                "normalized": _provenance_command(logical_create),
                "exit_code": create.returncode,
                "stdout": _stream_record(create.stdout, artifact=None),
                "stderr": _stream_record(create.stderr, artifact=None),
            },
            "verify": {
                "argv": [
                    "python",
                    "-m",
                    "receipt_extractor.main",
                    *logical_verify,
                ],
                "normalized": _provenance_command(logical_verify),
                "exit_code": verify.returncode,
                "stdout": _stream_record(
                    verify.stdout,
                    artifact="demo/evidence/run-verification.json",
                ),
                "stderr": _stream_record(verify.stderr, artifact=None),
            },
        },
        "bindings": [
            {
                "id": "input_batch",
                "label": "Exact input batch",
                "value": body["input_batch_digest"],
                "producer": "ordered canonical input descriptors",
                "run_field": "body.input_batch_digest",
            },
            {
                "id": "receipt_contract",
                "label": "Receipt contract v1",
                "value": contract["digest"],
                "producer": "domain-separated canonical contract JSON",
                "run_field": "body.contract.digest",
            },
            {
                "id": "replay_manifest_file",
                "label": "Raw replay manifest",
                "value": body["replay_manifest_file_sha256"],
                "producer": "SHA-256 of exact manifest file bytes",
                "run_field": "body.replay_manifest_file_sha256",
            },
            {
                "id": "run_id",
                "label": "Complete run body",
                "value": run_document["run_id"],
                "producer": "domain-separated canonical run body",
                "run_field": "run_id",
            },
        ],
        "verifier_edges": [
            {
                "binding": "input_batch",
                "from": "current preflight descriptors",
                "to": "manifest batch and run body",
            },
            {
                "binding": "receipt_contract",
                "from": "current receipt contract",
                "to": "run body contract",
            },
            {
                "binding": "replay_manifest_file",
                "from": "exact manifest file bytes",
                "to": "run body manifest digest",
            },
            {
                "binding": "run_id",
                "from": "complete canonical run body",
                "to": "stored run identity",
            },
        ],
        "verification_checks": [
            "bounded pinned JSON reads",
            "normal image preflight",
            "exact ordered input names",
            "strict typed output equality",
            "current receipt schema",
        ],
    }
    return {
        "run_output": run_bytes.decode("ascii"),
        "verification_output": verify.stdout,
        "source": source,
    }


def _wrapped_lines(text: str, *, width: int = 112) -> list[str]:
    lines: list[str] = []
    for raw_line in text.rstrip().splitlines():
        if not raw_line:
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                raw_line,
                width=width,
                subsequent_indent="  ",
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return lines


def _terminal_capture(
    *,
    title: str,
    command: str,
    output: str,
) -> Image.Image:
    command_lines = _wrapped_lines(f"$ {command}")
    output_lines = _wrapped_lines(output)
    line_height = 28
    height = max(
        430,
        112 + (len(command_lines) + len(output_lines) + 2) * line_height,
    )
    image = Image.new("RGB", (1440, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 24, 1416, height - 24),
        radius=24,
        fill=PANEL,
        outline="#293653",
        width=2,
    )
    for x, color in ((58, RED), (84, AMBER), (110, GREEN)):
        draw.ellipse((x - 8, 50, x + 8, 66), fill=color)
    draw.text((136, 43), title, font=_font(22), fill=TEXT)
    draw.line((42, 86, 1398, 86), fill="#293653", width=2)

    y = 108
    for line in command_lines:
        draw.text((58, y), line, font=_font(20), fill=GREEN)
        y += line_height
    y += 12
    for line in output_lines:
        color = CYAN if line.lstrip().startswith(("{", "}", "[", "]")) else TEXT
        draw.text((58, y), line, font=_font(20), fill=color)
        y += line_height
    return image


def _provenance_terminal_capture(
    *,
    create_command: str,
    verify_command: str,
    verification_output: str,
) -> Image.Image:
    create_lines = _wrapped_lines(f"$ {create_command}", width=105)
    verify_lines = _wrapped_lines(f"$ {verify_command}", width=105)
    output_lines = _wrapped_lines(verification_output, width=105)
    line_height = 28
    height = max(
        520,
        130
        + (len(create_lines) + len(verify_lines) + len(output_lines) + 4) * line_height,
    )
    image = Image.new("RGB", (1440, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 24, 1416, height - 24),
        radius=24,
        fill=PANEL,
        outline="#293653",
        width=2,
    )
    for x, color in ((58, RED), (84, AMBER), (110, GREEN)):
        draw.ellipse((x - 8, 50, x + 8, 66), fill=color)
    draw.text(
        (136, 43),
        "Actual content-addressed replay commands",
        font=_font(22),
        fill=TEXT,
    )
    draw.line((42, 86, 1398, 86), fill="#293653", width=2)

    y = 108
    for line in create_lines:
        draw.text((58, y), line, font=_font(20), fill=GREEN)
        y += line_height
    y += line_height
    for line in verify_lines:
        draw.text((58, y), line, font=_font(20), fill=GREEN)
        y += line_height
    y += 12
    for line in output_lines:
        color = CYAN if line.lstrip().startswith(("{", "}")) else TEXT
        draw.text((58, y), line, font=_font(20), fill=color)
        y += line_height
    return image


def _failure_gallery(evidence: _FailureEvidence) -> Image.Image:
    width, height = 1440, 1220
    image = Image.new("RGB", (width, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.text((64, 48), "Actual fail-closed CLI boundaries", font=_font(42), fill=TEXT)
    draw.text(
        (66, 106),
        (
            "Recorded CLI streams with normalized reproduction commands and "
            "synthetic fixtures."
        ),
        font=_font(22),
        fill=MUTED,
    )

    panel_width = 638
    panel_height = 452
    for index, case in enumerate(evidence["cases"]):
        column = index % 2
        row = index // 2
        x = 64 + column * 674
        y = 164 + row * 486
        draw.rounded_rectangle(
            (x, y, x + panel_width, y + panel_height),
            radius=24,
            fill=PANEL,
            outline="#293653",
            width=2,
        )
        draw.text((x + 28, y + 24), case["title"], font=_font(27), fill=TEXT)
        badge = f"exit {case['exit_code']}"
        badge_width = _text_width(draw, badge, size=19) + 32
        draw.rounded_rectangle(
            (x + panel_width - badge_width - 24, y + 22, x + panel_width - 24, y + 60),
            radius=14,
            fill="#3b2230",
            outline=RED,
            width=2,
        )
        draw.text(
            (x + panel_width - badge_width - 8, y + 31),
            badge,
            font=_font(19),
            fill=RED,
        )
        draw.text((x + 28, y + 76), case["id"], font=_font(16), fill=CYAN)

        cursor = y + 114
        for line in _wrapped_lines(case["invariant"], width=55):
            draw.text((x + 28, cursor), line, font=_font(18), fill=MUTED)
            cursor += 24
        cursor += 12
        draw.text(
            (x + 28, cursor),
            "$ reproduction command",
            font=_font(17),
            fill=GREEN,
        )
        cursor += 27
        for line in _wrapped_lines(case["reproduction_command"], width=55):
            draw.text((x + 28, cursor), line, font=_font(17), fill=GREEN)
            cursor += 23
        cursor += 12
        draw.text((x + 28, cursor), "stderr", font=_font(17), fill=AMBER)
        cursor += 27
        for line in _wrapped_lines(case["stderr"], width=55):
            draw.text((x + 28, cursor), line, font=_font(17), fill=TEXT)
            cursor += 23
        draw.text(
            (x + 28, y + panel_height - 42),
            "stdout: empty",
            font=_font(16),
            fill=MUTED,
        )

    mismatch = evidence["replay_mismatch_output"]
    preserved = evidence["preserved_output"]
    mismatch_absent = not mismatch["exists_before"] and not mismatch["exists_after"]
    draw.text(
        (66, 1146),
        (
            "Replay mismatch output absent before and after: "
            f"{str(mismatch_absent).lower()}"
        ),
        font=_font(19),
        fill=GREEN,
    )
    draw.text(
        (66, 1179),
        (f"No-clobber SHA-256 before = after: {preserved['sha256_after'][:16]}…"),
        font=_font(19),
        fill=GREEN,
    )
    return image


def _receipt_montage(input_dir: Path) -> Image.Image:
    image = Image.new("RGB", (1440, 900), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.text((72, 54), "Deterministic synthetic inputs", font=_font(42), fill=TEXT)
    draw.text(
        (74, 112),
        "Real PNG + lossless WebP decoded by the production preflight",
        font=_font(23),
        fill=MUTED,
    )
    paths = (input_dir / "cafe-lumen.png", input_dir / "metro-line.webp")
    captions = ("cafe-lumen.png", "metro-line.webp")
    for index, (path, caption) in enumerate(zip(paths, captions, strict=True)):
        with Image.open(path) as receipt:
            rendered = ImageOps.contain(receipt.convert("RGB"), (460, 690))
        x = 196 + index * 660
        y = 170
        draw.rounded_rectangle(
            (x - 20, y - 20, x + 480, y + 710),
            radius=26,
            fill=PANEL,
            outline="#293653",
            width=2,
        )
        image.paste(rendered, (x + (460 - rendered.width) // 2, y))
        caption_width = _text_width(draw, caption, size=23)
        draw.text(
            (x + (460 - caption_width) // 2, 810),
            caption,
            font=_font(23),
            fill=CYAN,
        )
    return image


def _evaluation_fixture_montage(
    input_dir: Path,
    capture: _EvaluationCapture,
) -> Image.Image:
    width, height = 1440, 1040
    image = Image.new("RGB", (width, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.text(
        (62, 44),
        "Eight content-addressed evaluation inputs",
        font=_font(40),
        fill=TEXT,
    )
    draw.text(
        (64, 98),
        "Actual PNG bytes loaded by production preflight and bound into the suite",
        font=_font(22),
        fill=MUTED,
    )

    for index, case in enumerate(capture.suite.body.cases):
        column = index % 4
        row = index // 4
        x = 54 + column * 348
        y = 154 + row * 424
        draw.rounded_rectangle(
            (x, y, x + 316, y + 390),
            radius=22,
            fill=PANEL,
            outline="#293653",
            width=2,
        )
        with Image.open(input_dir / case.input.name) as receipt:
            rendered = ImageOps.contain(receipt.convert("RGB"), (226, 282))
        image.paste(
            rendered,
            (
                x + (316 - rendered.width) // 2,
                y + 18 + (282 - rendered.height) // 2,
            ),
        )
        draw.text(
            (x + 18, y + 314),
            case.case_id,
            font=_font(17),
            fill=CYAN,
        )
        draw.text(
            (x + 18, y + 341),
            f"{case.input.size_bytes} bytes · sha256 {case.input.sha256[:12]}…",
            font=_font(15),
            fill=MUTED,
        )
        draw.text(
            (x + 18, y + 366),
            f"{case.input.width}x{case.input.height} PNG",
            font=_font(15),
            fill=MUTED,
        )

    draw.text(
        (64, 1000),
        f"suite_id  {capture.suite.suite_id}",
        font=_font(17),
        fill=GREEN,
    )
    return image


def _evaluation_terminal_capture(capture: _EvaluationCapture) -> Image.Image:
    evaluate_lines = _wrapped_lines(
        "$ PYTHONPATH=src python -m receipt_extractor.evaluation_cli evaluate "
        "demo/evaluation-suite.json --format text",
        width=105,
    )
    output_lines = _wrapped_lines(capture.text_output, width=105)
    verify_lines = _wrapped_lines(
        "$ PYTHONPATH=src python -m receipt_extractor.evaluation_cli verify "
        "demo/evaluation-suite.json demo/evidence/evaluation-receipt.json",
        width=105,
    )
    verification_lines = _wrapped_lines(capture.verification_output, width=105)
    line_height = 27
    height = max(
        760,
        130
        + (
            len(evaluate_lines)
            + len(output_lines)
            + len(verify_lines)
            + len(verification_lines)
            + 5
        )
        * line_height,
    )
    image = Image.new("RGB", (1440, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 24, 1416, height - 24),
        radius=24,
        fill=PANEL,
        outline="#293653",
        width=2,
    )
    for x, color in ((58, RED), (84, AMBER), (110, GREEN)):
        draw.ellipse((x - 8, 50, x + 8, 66), fill=color)
    draw.text(
        (136, 43),
        "Actual offline evaluation and full recomputation",
        font=_font(22),
        fill=TEXT,
    )
    draw.line((42, 86, 1398, 86), fill="#293653", width=2)

    y = 108
    for line in evaluate_lines:
        draw.text((58, y), line, font=_font(19), fill=GREEN)
        y += line_height
    y += 10
    for line in output_lines:
        color = AMBER if line.startswith("Authored ") else TEXT
        draw.text((58, y), line, font=_font(19), fill=color)
        y += line_height
    y += line_height
    for line in verify_lines:
        draw.text((58, y), line, font=_font(19), fill=GREEN)
        y += line_height
    y += 10
    for line in verification_lines:
        color = CYAN if line.lstrip().startswith(("{", "}")) else TEXT
        draw.text((58, y), line, font=_font(19), fill=color)
        y += line_height
    return image


def _collect_test_count(repository: Path) -> int:
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONPATH"] = str(repository / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    matched = re.search(r"(\d+) tests? collected", completed.stdout)
    if matched is None:
        raise RuntimeError("could not derive the collected test count")
    return int(matched.group(1))


def _coverage_summary(coverage_path: Path, *, test_count: int) -> dict[str, Any]:
    decoded = json.loads(coverage_path.read_text(encoding="utf-8"))
    selected = (
        ("artifact_io", "src/receipt_extractor/artifact_io.py"),
        ("evaluation", "src/receipt_extractor/evaluation.py"),
        ("evaluation_cli", "src/receipt_extractor/evaluation_cli.py"),
        ("file_io", "src/receipt_extractor/file_io.py"),
        ("main", "src/receipt_extractor/main.py"),
        ("provenance", "src/receipt_extractor/provenance.py"),
        ("replay", "src/receipt_extractor/replay.py"),
        ("gpt", "src/receipt_extractor/gpt.py"),
        ("schema", "src/receipt_extractor/schema.py"),
    )
    files = decoded["files"]
    modules = [
        {
            "module": label,
            "percent": round(float(files[path]["summary"]["percent_covered"]), 2),
        }
        for label, path in selected
    ]
    return {
        "schema_version": 1,
        "command": "make check",
        "test_count": test_count,
        "combined_percent": round(
            float(decoded["totals"]["percent_covered"]),
            2,
        ),
        "modules": modules,
    }


def _coverage_png(summary: dict[str, Any]) -> Image.Image:
    width = 1440
    height = 284 + len(summary["modules"]) * 108
    image = Image.new("RGB", (width, height), CANVAS)
    draw = ImageDraw.Draw(image)
    draw.text((72, 54), "Offline verification coverage", font=_font(42), fill=TEXT)
    draw.text(
        (74, 112),
        (
            f"{summary['test_count']} tests  |  "
            f"{summary['combined_percent']:.2f}% combined line/branch coverage"
        ),
        font=_font(23),
        fill=MUTED,
    )
    modules = summary["modules"]
    for index, module in enumerate(modules):
        y = 188 + index * 108
        label = str(module["module"])
        percent = float(module["percent"])
        draw.text((78, y + 14), label, font=_font(25), fill=TEXT)
        draw.rounded_rectangle(
            (260, y, 1280, y + 58),
            radius=18,
            fill=PANEL_LIGHT,
        )
        fill_width = int(1020 * percent / 100)
        color = GREEN if percent >= 90 else AMBER
        draw.rounded_rectangle(
            (260, y, 260 + fill_width, y + 58),
            radius=18,
            fill=color,
        )
        draw.text(
            (1300, y + 14),
            f"{percent:.2f}%",
            font=_font(22),
            fill=color,
        )
    draw.text(
        (78, height - 56),
        "Generated from coverage.py JSON after the full synthetic/offline suite.",
        font=_font(20),
        fill=MUTED,
    )
    return image


def _svg_text(
    *,
    x: int,
    y: int,
    text: str,
    size: int,
    color: str = TEXT,
    anchor: str = "middle",
    weight: int = 600,
) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="system-ui, sans-serif" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}">{escaped}</text>'
    )


def _svg_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _evaluation_scorecard_svg(capture: _EvaluationCapture) -> str:
    metrics = capture.report.body.metrics
    exact_records = metrics.record_exact_field_histogram[len(evaluation.FIELD_ORDER)]
    width, height = 1440, 770
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Authored negative-control evaluation scorecard</title>',
        (
            '<desc id="description">Exact aggregate counts produced by the '
            "offline evaluator for eight repository-authored synthetic cases. "
            "This is evaluator calibration, not model accuracy.</desc>"
        ),
        f'<rect width="{width}" height="{height}" fill="{CANVAS}"/>',
        _svg_text(
            x=52,
            y=62,
            text="Exact evaluator calibration, without rounded scores",
            size=34,
            anchor="start",
        ),
        _svg_text(
            x=54,
            y=101,
            text=(
                "Every number below is read from the validated aggregate "
                "evaluation receipt."
            ),
            size=19,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
    ]
    summary_cards = (
        (52, "CASES", str(metrics.case_count), CYAN),
        (
            360,
            "FIELD AGREEMENT",
            f"{metrics.all_fields.exact} / {metrics.all_fields.total}",
            GREEN,
        ),
        (
            770,
            "EXACT RECORDS",
            f"{exact_records} / {metrics.case_count}",
            AMBER,
        ),
        (
            1080,
            "HISTOGRAM 0..4",
            "["
            + ", ".join(str(value) for value in metrics.record_exact_field_histogram)
            + "]",
            PURPLE,
        ),
    )
    card_widths = (270, 370, 270, 308)
    for (x, label, value, accent), card_width in zip(
        summary_cards, card_widths, strict=True
    ):
        parts.extend(
            (
                (
                    f'<rect x="{x}" y="136" width="{card_width}" height="112" '
                    f'rx="20" fill="{PANEL}" stroke="{accent}" stroke-width="2"/>'
                ),
                _svg_text(
                    x=x + 22,
                    y=171,
                    text=label,
                    size=15,
                    color=MUTED,
                    anchor="start",
                    weight=500,
                ),
                _svg_text(
                    x=x + 22,
                    y=219,
                    text=value,
                    size=27,
                    color=accent,
                    anchor="start",
                ),
            )
        )

    legend = (
        ("exact", GREEN),
        ("omission", AMBER),
        ("spurious", PURPLE),
        ("substitution", RED),
    )
    for index, (label, color) in enumerate(legend):
        x = 330 + index * 230
        parts.extend(
            (
                f'<rect x="{x}" y="278" width="20" height="20" rx="5" fill="{color}"/>',
                _svg_text(
                    x=x + 31,
                    y=294,
                    text=label,
                    size=16,
                    color=MUTED,
                    anchor="start",
                    weight=500,
                ),
            )
        )

    colors = (GREEN, AMBER, PURPLE, RED)
    for row, item in enumerate(metrics.per_field):
        y = 330 + row * 82
        counts = (
            item.outcomes.exact,
            item.outcomes.omission,
            item.outcomes.spurious,
            item.outcomes.substitution,
        )
        parts.append(
            _svg_text(
                x=54,
                y=y + 35,
                text=item.field,
                size=22,
                anchor="start",
            )
        )
        cursor = 250
        for count, color in zip(counts, colors, strict=True):
            segment_width = 720 * count // metrics.case_count
            if segment_width:
                parts.extend(
                    (
                        (
                            f'<rect x="{cursor}" y="{y}" width="{segment_width}" '
                            f'height="48" rx="10" fill="{color}"/>'
                        ),
                        _svg_text(
                            x=cursor + segment_width // 2,
                            y=y + 32,
                            text=str(count),
                            size=18,
                            color=CANVAS,
                        ),
                    )
                )
            cursor += segment_width
        parts.append(
            _svg_text(
                x=1000,
                y=y + 32,
                text="counts  " + " / ".join(str(count) for count in counts),
                size=16,
                color=MUTED,
                anchor="start",
                weight=400,
            )
        )
    parts.extend(
        (
            _svg_text(
                x=52,
                y=712,
                text=(
                    "Authored negative-control calibration · exact typed equality "
                    "· not live-model accuracy"
                ),
                size=19,
                color=AMBER,
                anchor="start",
                weight=500,
            ),
            _svg_text(
                x=52,
                y=744,
                text=f"report_id  {capture.report.report_id}",
                size=15,
                color=MUTED,
                anchor="start",
                weight=400,
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _evaluation_confusion_svg(capture: _EvaluationCapture) -> str:
    confusion = capture.report.body.metrics.category_confusion
    width, height = 1440, 940
    grid_x, grid_y, cell = 430, 216, 88
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Exact category confusion matrix</title>',
        (
            '<desc id="description">A fixed seven-label category confusion '
            "matrix read from the aggregate evaluation receipt, with truth on "
            "rows and authored control candidates on columns.</desc>"
        ),
        f'<rect width="{width}" height="{height}" fill="{CANVAS}"/>',
        _svg_text(
            x=52,
            y=62,
            text="Category confusion from the aggregate receipt",
            size=34,
            anchor="start",
        ),
        _svg_text(
            x=54,
            y=101,
            text=(
                "Truth on rows · authored negative control on columns · "
                "all seven fixed labels retained"
            ),
            size=19,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
        _svg_text(
            x=grid_x + cell * len(confusion.labels) // 2,
            y=145,
            text="CANDIDATE",
            size=18,
            color=CYAN,
        ),
        _svg_text(
            x=75,
            y=grid_y + cell * len(confusion.labels) // 2,
            text="TRUTH",
            size=18,
            color=CYAN,
            anchor="start",
        ),
    ]
    column_lines = {
        "Office Supplies": ("Office", "Supplies"),
        "Entertainment": ("Entertain-", "ment"),
    }
    for column, label in enumerate(confusion.labels):
        lines = column_lines.get(label, (label,))
        for line_index, line in enumerate(lines):
            parts.append(
                _svg_text(
                    x=grid_x + column * cell + cell // 2,
                    y=176 + line_index * 18,
                    text=line,
                    size=13,
                    color=MUTED,
                    weight=500,
                )
            )
    for row, (label, values) in enumerate(
        zip(confusion.labels, confusion.matrix, strict=True)
    ):
        y = grid_y + row * cell
        parts.append(
            _svg_text(
                x=400,
                y=y + 53,
                text=label,
                size=16,
                color=MUTED,
                anchor="end",
                weight=500,
            )
        )
        for column, count in enumerate(values):
            x = grid_x + column * cell
            fill = PANEL_LIGHT if count == 0 else (PURPLE if count == 1 else GREEN)
            text_color = MUTED if count == 0 else CANVAS
            truth_attribute = _svg_attribute(label)
            candidate_attribute = _svg_attribute(confusion.labels[column])
            parts.extend(
                (
                    (
                        f'<rect x="{x}" y="{y}" width="{cell - 4}" '
                        f'height="{cell - 4}" rx="14" fill="{fill}" '
                        f'stroke="{PANEL}" stroke-width="2" '
                        f'data-truth="{truth_attribute}" '
                        f'data-candidate="{candidate_attribute}" '
                        f'data-count="{count}"/>'
                    ),
                    _svg_text(
                        x=x + (cell - 4) // 2,
                        y=y + 53,
                        text=str(count),
                        size=21,
                        color=text_color,
                    ),
                )
            )
    nonzero = sum(1 for row in confusion.matrix for count in row if count != 0)
    parts.extend(
        (
            _svg_text(
                x=52,
                y=875,
                text=(
                    f"{capture.report.body.metrics.case_count} observations · "
                    f"{nonzero} nonzero cells · integer counts only"
                ),
                size=19,
                color=GREEN,
                anchor="start",
            ),
            _svg_text(
                x=52,
                y=910,
                text=(
                    "Sparse aggregate category cells may disclose authored fixture "
                    "pairs; this public suite is synthetic."
                ),
                size=17,
                color=MUTED,
                anchor="start",
                weight=400,
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _evaluation_bindings_svg(capture: _EvaluationCapture) -> str:
    suite = capture.suite
    report = capture.report
    nodes = (
        (
            50,
            188,
            620,
            126,
            "Production-loaded inputs",
            f"{len(suite.body.cases)} PNG descriptors",
            suite.body.input_batch_digest,
            CYAN,
        ),
        (
            770,
            188,
            620,
            126,
            "Content-addressed suite",
            suite.body.name,
            suite.suite_id,
            PURPLE,
        ),
        (
            50,
            414,
            620,
            126,
            "Pinned evaluator semantics",
            report.body.evaluator.id,
            report.body.evaluator.digest,
            AMBER,
        ),
        (
            770,
            414,
            620,
            126,
            "Aggregate evaluation receipt",
            report.body.mode,
            report.report_id,
            GREEN,
        ),
    )
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="760" '
        'viewBox="0 0 1440 760" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Synthetic evaluation identity bindings</title>',
        (
            '<desc id="description">Exact image descriptors bind into a '
            "content-addressed suite. Pinned evaluator semantics and the full "
            "suite are recomputed into one aggregate report, which the offline "
            "verifier compares completely.</desc>"
        ),
        f'<rect width="1440" height="760" fill="{CANVAS}"/>',
        "<defs>",
        (
            f'<marker id="evaluation-arrow" viewBox="0 0 10 10" refX="9" '
            'refY="5" markerWidth="8" markerHeight="8" orient="auto">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MUTED}"/></marker>'
        ),
        "</defs>",
        _svg_text(
            x=52,
            y=62,
            text="From exact fixture bytes to a recomputed aggregate receipt",
            size=34,
            anchor="start",
        ),
        _svg_text(
            x=54,
            y=101,
            text=(
                "Domain-separated SHA-256 identities are mismatch guards, "
                "not signatures or timestamps."
            ),
            size=19,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
        (
            f'<path d="M 670 251 L 770 251" stroke="{MUTED}" stroke-width="3" '
            'fill="none" marker-end="url(#evaluation-arrow)"/>'
        ),
        (
            f'<path d="M 1080 314 L 1080 414" stroke="{MUTED}" stroke-width="3" '
            'fill="none" marker-end="url(#evaluation-arrow)"/>'
        ),
        (
            f'<path d="M 670 477 L 770 477" stroke="{MUTED}" stroke-width="3" '
            'fill="none" marker-end="url(#evaluation-arrow)"/>'
        ),
    ]
    for x, y, node_width, node_height, title, subtitle, digest, accent in nodes:
        parts.extend(
            (
                (
                    f'<rect x="{x}" y="{y}" width="{node_width}" '
                    f'height="{node_height}" rx="22" fill="{PANEL}" '
                    f'stroke="{accent}" stroke-width="3"/>'
                ),
                _svg_text(
                    x=x + 24,
                    y=y + 38,
                    text=title,
                    size=22,
                    color=accent,
                    anchor="start",
                ),
                _svg_text(
                    x=x + 24,
                    y=y + 70,
                    text=subtitle,
                    size=16,
                    color=MUTED,
                    anchor="start",
                    weight=400,
                ),
                _svg_text(
                    x=x + 24,
                    y=y + 102,
                    text=digest,
                    size=14,
                    anchor="start",
                    weight=500,
                ),
            )
        )
    parts.extend(
        (
            '<rect x="220" y="622" width="1000" height="78" rx="20" '
            f'fill="{PANEL}" stroke="{GREEN}" stroke-width="3"/>',
            _svg_text(
                x=720,
                y=653,
                text="Offline verifier",
                size=21,
                color=GREEN,
            ),
            _svg_text(
                x=720,
                y=682,
                text=(
                    "reload suite + reload report + recompute every metric + "
                    "compare complete canonical report bytes"
                ),
                size=16,
                color=MUTED,
                weight=400,
            ),
            (
                f'<path d="M 1080 540 C 1080 590 980 600 930 622" '
                f'stroke="{MUTED}" stroke-width="3" fill="none" '
                'marker-end="url(#evaluation-arrow)"/>'
            ),
            (
                f'<path d="M 1360 314 C 1410 370 1400 590 1140 622" '
                f'stroke="{MUTED}" stroke-width="2" stroke-dasharray="8 7" '
                'fill="none" marker-end="url(#evaluation-arrow)"/>'
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _architecture_svg() -> str:
    nodes = (
        (32, 304, 190, 112, "Receipt batch", "PNG / JPEG / WebP", CYAN),
        (270, 304, 205, 112, "Pinned preflight", "file_io.py", GREEN),
        (535, 304, 190, 112, "Mode router", "main.py", PURPLE),
        (790, 86, 190, 104, "Dry-run", "audit metadata", PURPLE),
        (790, 304, 190, 112, "Exact replay", "replay.py", AMBER),
        (790, 526, 190, 112, "Live Responses", "gpt.py", RED),
        (1040, 304, 190, 112, "Typed boundary", "ReceiptFields", GREEN),
        (1270, 304, 145, 112, "Result sink", "0600 / stdout", CYAN),
        (270, 690, 205, 104, "Replay manifest", "strict JSON + digest", AMBER),
        (790, 690, 190, 104, "Run builder", "provenance.py", CYAN),
        (1040, 690, 190, 104, "Run verifier", "four bindings", GREEN),
        (1270, 690, 145, 104, "Fixed stdout", "verified: true", GREEN),
    )
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="880" '
        'viewBox="0 0 1440 880" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Auditable Receipt Extractor architecture</title>',
        (
            '<desc id="description">The real CLI flow from bounded receipt inputs '
            "through preflight and three execution modes, plus content-addressed "
            "replay run creation and local four-binding verification.</desc>"
        ),
        f'<rect width="1440" height="880" fill="{CANVAS}"/>',
        "<defs>",
        (
            f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MUTED}"/></marker>'
        ),
        "</defs>",
        _svg_text(
            x=52,
            y=66,
            text="One preflight, three modes, one local provenance verifier",
            size=34,
            anchor="start",
        ),
        _svg_text(
            x=54,
            y=105,
            text=(
                "Replay can emit a normal result or one content-addressed "
                "run bundle; verification never imports OpenAI."
            ),
            size=20,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
    ]
    arrows = (
        "M 222 360 L 270 360",
        "M 475 360 L 535 360",
        "M 725 342 L 790 138",
        "M 725 360 L 790 360",
        "M 725 378 L 790 582",
        "M 475 742 C 610 742 640 430 790 390",
        "M 980 360 L 1040 360",
        "M 980 582 C 1020 582 1010 402 1040 402",
        "M 1230 360 L 1270 360",
        "M 980 390 C 1020 470 880 610 885 690",
        "M 475 742 L 790 742",
        "M 980 742 L 1040 742",
        "M 475 768 C 700 850 1110 850 1135 794",
        "M 1230 742 L 1270 742",
    )
    parts.extend(
        f'<path d="{path}" stroke="{MUTED}" '
        'stroke-width="3" fill="none" marker-end="url(#arrow)"/>'
        for path in arrows
    )
    for x, y, width, height, title, subtitle, accent in nodes:
        parts.extend(
            (
                (
                    f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
                    f'rx="22" fill="{PANEL}" stroke="{accent}" stroke-width="3"/>'
                ),
                _svg_text(
                    x=x + width // 2,
                    y=y + 50,
                    text=title,
                    size=22,
                ),
                _svg_text(
                    x=x + width // 2,
                    y=y + 82,
                    text=subtitle,
                    size=16,
                    color=MUTED,
                    weight=400,
                ),
            )
        )
    parts.extend(
        (
            _svg_text(
                x=720,
                y=842,
                text=(
                    "Offline replay provenance: synthetic evidence, no key, "
                    "no provider request"
                ),
                size=18,
                color=AMBER,
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _coverage_svg(summary: dict[str, Any]) -> str:
    width = 1080
    height = 206 + len(summary["modules"]) * 82
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Offline verification coverage</title>',
        (
            f'<desc id="description">{summary["test_count"]} tests with '
            f"{summary['combined_percent']:.2f}% combined line and branch "
            "coverage.</desc>"
        ),
        f'<rect width="{width}" height="{height}" rx="24" fill="{CANVAS}"/>',
        _svg_text(
            x=50,
            y=58,
            text="Offline verification coverage",
            size=32,
            anchor="start",
        ),
        _svg_text(
            x=52,
            y=94,
            text=(
                f"{summary['test_count']} tests  |  "
                f"{summary['combined_percent']:.2f}% combined line/branch"
            ),
            size=18,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
    ]
    for index, module in enumerate(summary["modules"]):
        y = 132 + index * 82
        percent = float(module["percent"])
        bar_width = round(710 * percent / 100)
        color = GREEN if percent >= 90 else AMBER
        parts.extend(
            (
                _svg_text(
                    x=52,
                    y=y + 31,
                    text=str(module["module"]),
                    size=20,
                    anchor="start",
                ),
                (
                    f'<rect x="180" y="{y}" width="710" height="42" rx="13" '
                    f'fill="{PANEL_LIGHT}"/>'
                ),
                (
                    f'<rect x="180" y="{y}" width="{bar_width}" height="42" '
                    f'rx="13" fill="{color}"/>'
                ),
                _svg_text(
                    x=1018,
                    y=y + 29,
                    text=f"{percent:.2f}%",
                    size=18,
                    color=color,
                    anchor="end",
                ),
            )
        )
    parts.extend(
        (
            _svg_text(
                x=52,
                y=height - 32,
                text="Source: coverage.py JSON emitted by make check",
                size=16,
                color=MUTED,
                anchor="start",
                weight=400,
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _provenance_svg(source: dict[str, Any]) -> str:
    bindings = source["bindings"]
    edges = source["verifier_edges"]
    checks = source["verification_checks"]
    if (
        not isinstance(bindings, list)
        or not isinstance(edges, list)
        or not isinstance(checks, list)
    ):
        raise RuntimeError("provenance source does not match renderer schema")
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="950" '
        'viewBox="0 0 1440 950" role="img" '
        'aria-labelledby="title description">',
        '<title id="title">Four content-addressed replay run bindings</title>',
        (
            '<desc id="description">Four locally recomputed values connect '
            "current inputs, the receipt contract, exact manifest bytes, and "
            "the complete run body to one verified replay run. Non-hash checks "
            "also enforce ordered names, typed outputs, bounded reads, image "
            "preflight, and the current schema.</desc>"
        ),
        f'<rect width="1440" height="950" fill="{CANVAS}"/>',
        "<defs>",
        (
            f'<marker id="binding-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MUTED}"/></marker>'
        ),
        "</defs>",
        _svg_text(
            x=52,
            y=62,
            text="Four bindings, recomputed by the local verifier",
            size=34,
            anchor="start",
        ),
        _svg_text(
            x=54,
            y=101,
            text=(
                "Every digest below is read from the generated run and "
                "provenance source; none is a signature or model claim."
            ),
            size=19,
            color=MUTED,
            anchor="start",
            weight=400,
        ),
    ]
    for index, (binding, edge) in enumerate(zip(bindings, edges, strict=True)):
        if binding["id"] != edge["binding"]:
            raise RuntimeError("provenance binding and verifier edge order drifted")
        y = 148 + index * 150
        accent = (CYAN, PURPLE, AMBER, GREEN)[index]
        parts.extend(
            (
                (
                    f'<rect x="50" y="{y}" width="330" height="112" rx="20" '
                    f'fill="{PANEL}" stroke="{accent}" stroke-width="3"/>'
                ),
                _svg_text(
                    x=72,
                    y=y + 39,
                    text=str(binding["label"]),
                    size=22,
                    color=accent,
                    anchor="start",
                ),
                _svg_text(
                    x=72,
                    y=y + 74,
                    text=str(edge["from"]),
                    size=16,
                    color=MUTED,
                    anchor="start",
                    weight=400,
                ),
                (
                    f'<path d="M 380 {y + 56} L 445 {y + 56}" '
                    f'stroke="{MUTED}" stroke-width="3" fill="none" '
                    'marker-end="url(#binding-arrow)"/>'
                ),
                (
                    f'<rect x="445" y="{y}" width="725" height="112" rx="20" '
                    f'fill="{PANEL}" stroke="{accent}" stroke-width="3"/>'
                ),
                _svg_text(
                    x=470,
                    y=y + 34,
                    text=str(binding["producer"]),
                    size=17,
                    anchor="start",
                ),
                _svg_text(
                    x=470,
                    y=y + 67,
                    text=str(binding["value"]),
                    size=15,
                    color=accent,
                    anchor="start",
                    weight=500,
                ),
                _svg_text(
                    x=470,
                    y=y + 94,
                    text=f"run field: {binding['run_field']}",
                    size=15,
                    color=MUTED,
                    anchor="start",
                    weight=400,
                ),
                (
                    f'<path d="M 1170 {y + 56} L 1232 {y + 56}" '
                    f'stroke="{MUTED}" stroke-width="3" fill="none" '
                    'marker-end="url(#binding-arrow)"/>'
                ),
            )
        )
    parts.extend(
        (
            '<rect x="50" y="730" width="1120" height="145" rx="22" '
            f'fill="{PANEL}" stroke="{PURPLE}" stroke-width="3"/>',
            _svg_text(
                x=72,
                y=764,
                text="Non-hash equality checks",
                size=21,
                color=PURPLE,
                anchor="start",
            ),
        )
    )
    badge_positions = (
        (72, 783, 300),
        (386, 783, 260),
        (660, 783, 285),
        (72, 827, 300),
        (386, 827, 300),
    )
    if len(checks) != len(badge_positions):
        raise RuntimeError("provenance verification check count drifted")
    for check, (x, y, width) in zip(checks, badge_positions, strict=True):
        parts.extend(
            (
                (
                    f'<rect x="{x}" y="{y}" width="{width}" height="34" rx="12" '
                    f'fill="{PANEL_LIGHT}" stroke="{MUTED}" stroke-width="1"/>'
                ),
                _svg_text(
                    x=x + 14,
                    y=y + 23,
                    text=str(check),
                    size=14,
                    anchor="start",
                    weight=500,
                ),
            )
        )
    parts.extend(
        (
            '<path d="M 1170 807 L 1232 807" '
            f'stroke="{MUTED}" stroke-width="3" fill="none" '
            'marker-end="url(#binding-arrow)"/>',
            '<rect x="1232" y="148" width="158" height="727" rx="24" '
            f'fill="{PANEL}" stroke="{GREEN}" stroke-width="3"/>',
            _svg_text(
                x=1311,
                y=450,
                text="Local",
                size=25,
                color=GREEN,
            ),
            _svg_text(
                x=1311,
                y=488,
                text="verifier",
                size=25,
                color=GREEN,
            ),
            _svg_text(
                x=1311,
                y=544,
                text="verified:",
                size=17,
                color=MUTED,
                weight=400,
            ),
            _svg_text(
                x=1311,
                y=572,
                text="true",
                size=21,
                color=GREEN,
            ),
            _svg_text(
                x=52,
                y=918,
                text=(
                    "Synthetic replay-only evidence · no provider request · "
                    "not authenticity"
                ),
                size=18,
                color=MUTED,
                anchor="start",
                weight=400,
            ),
            "</svg>",
        )
    )
    return "".join(parts)


def _generated_inventory(output_root: Path) -> tuple[str, ...]:
    paths = (
        *(path for path in (output_root / "demo").rglob("*") if path.is_file()),
        *(
            path
            for path in (output_root / "docs" / "assets").rglob("*")
            if path.is_file()
        ),
    )
    return tuple(sorted(path.relative_to(output_root).as_posix() for path in paths))


def _finalize_provenance_source(
    *,
    repository: Path,
    output_root: Path,
    source: dict[str, Any],
) -> None:
    source_path = output_root / "demo" / "evidence" / "provenance-source.json"
    generated_before_source = tuple(
        path
        for path in EXPECTED_GENERATED_FILES
        if path != "demo/evidence/provenance-source.json"
    )
    actual_before_source = tuple(
        path
        for path in _generated_inventory(output_root)
        if path != "demo/evidence/provenance-source.json"
    )
    if actual_before_source != generated_before_source:
        raise RuntimeError("generated evidence inventory does not match its allowlist")

    source["artifacts"] = [
        _file_record(output_root / logical_path, logical_path=logical_path)
        for logical_path in generated_before_source
    ]
    source["sources"] = [
        _file_record(repository / logical_path, logical_path=logical_path)
        for logical_path in PROVENANCE_SOURCE_FILES
    ]
    _write_json(source_path, source)
    if _generated_inventory(output_root) != EXPECTED_GENERATED_FILES:
        raise RuntimeError("final evidence inventory does not match its allowlist")


def _gif_frame(source: Image.Image, *, label: str) -> Image.Image:
    frame = Image.new("RGB", (1200, 760), CANVAS)
    contained = ImageOps.contain(source.convert("RGB"), (1120, 650))
    frame.paste(
        contained,
        ((1200 - contained.width) // 2, 70 + (650 - contained.height) // 2),
    )
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((32, 20, 1168, 64), radius=16, fill=PANEL)
    draw.text((56, 31), label, font=_font(21), fill=TEXT)
    return frame


def _save_assets(
    *,
    asset_dir: Path,
    input_dir: Path,
    evaluation_input_dir: Path,
    help_output: str,
    dry_run_output: str,
    replay_output: str,
    coverage: dict[str, Any],
    failures: _FailureEvidence,
    provenance_capture: _ProvenanceCapture,
    evaluation_capture: _EvaluationCapture,
) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    montage = _receipt_montage(input_dir)
    help_capture = _terminal_capture(
        title="Actual source CLI surface",
        command="PYTHONPATH=src python -m receipt_extractor.main --help",
        output=help_output,
    )
    dry_capture = _terminal_capture(
        title="Actual dry-run output",
        command=(
            "PYTHONPATH=src python -m receipt_extractor.main demo/inputs --dry-run"
        ),
        output=dry_run_output,
    )
    replay_capture = _terminal_capture(
        title="Actual exact-batch replay output",
        command=(
            "PYTHONPATH=src python -m receipt_extractor.main demo/inputs "
            "--replay demo/replay-manifest.json --stdout"
        ),
        output=replay_output,
    )
    coverage_image = _coverage_png(coverage)
    failure_image = _failure_gallery(failures)
    source = provenance_capture["source"]
    commands = source["commands"]
    if not isinstance(commands, dict):
        raise RuntimeError("provenance commands do not match renderer schema")
    create_command = commands["create"]
    verify_command = commands["verify"]
    if not isinstance(create_command, dict) or not isinstance(verify_command, dict):
        raise RuntimeError("provenance command records do not match renderer schema")
    provenance_image = _provenance_terminal_capture(
        create_command=str(create_command["normalized"]),
        verify_command=str(verify_command["normalized"]),
        verification_output=provenance_capture["verification_output"],
    )
    evaluation_fixtures = _evaluation_fixture_montage(
        evaluation_input_dir,
        evaluation_capture,
    )
    evaluation_terminal = _evaluation_terminal_capture(evaluation_capture)

    raster_assets = {
        "demo-receipts.png": montage,
        "cli-help.png": help_capture,
        "cli-dry-run.png": dry_capture,
        "cli-replay.png": replay_capture,
        "coverage.png": coverage_image,
        "failure-boundaries.png": failure_image,
        "cli-provenance.png": provenance_image,
        "evaluation-fixtures.png": evaluation_fixtures,
        "cli-evaluation.png": evaluation_terminal,
    }
    for name, image in raster_assets.items():
        image.save(asset_dir / name, format="PNG", compress_level=9)

    (asset_dir / "architecture.svg").write_text(
        _architecture_svg(),
        encoding="utf-8",
    )
    (asset_dir / "coverage.svg").write_text(
        _coverage_svg(coverage),
        encoding="utf-8",
    )
    (asset_dir / "provenance-bindings.svg").write_text(
        _provenance_svg(source),
        encoding="utf-8",
    )
    (asset_dir / "evaluation-scorecard.svg").write_text(
        _evaluation_scorecard_svg(evaluation_capture),
        encoding="utf-8",
    )
    (asset_dir / "evaluation-confusion.svg").write_text(
        _evaluation_confusion_svg(evaluation_capture),
        encoding="utf-8",
    )
    (asset_dir / "evaluation-bindings.svg").write_text(
        _evaluation_bindings_svg(evaluation_capture),
        encoding="utf-8",
    )
    frames = [
        _gif_frame(montage, label="1 / 7  Generate explicit synthetic receipts"),
        _gif_frame(help_capture, label="2 / 7  Inspect the real CLI contract"),
        _gif_frame(dry_capture, label="3 / 7  Preflight every input locally"),
        _gif_frame(replay_capture, label="4 / 7  Reproduce the exact batch offline"),
        _gif_frame(
            provenance_image,
            label="5 / 7  Create and verify a content-addressed run",
        ),
        _gif_frame(failure_image, label="6 / 7  Exercise fail-closed boundaries"),
        _gif_frame(coverage_image, label="7 / 7  Verify the complete offline gate"),
    ]
    frames[0].save(
        asset_dir / "demo.gif",
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=(2200, 2200, 2700, 3000, 3200, 3000, 2600),
        loop=0,
        disposal=2,
        optimize=False,
    )


def capture(
    *,
    repository: Path,
    output_root: Path,
    coverage_path: Path,
) -> None:
    demo_dir = output_root / "demo"
    input_dir = demo_dir / "inputs"
    evaluation_input_dir = demo_dir / "evaluation-inputs"
    evidence_dir = demo_dir / "evidence"
    asset_dir = output_root / "docs" / "assets"
    _save_inputs(input_dir)
    evaluation_specs = _evaluation_fixture_specs()
    _save_evaluation_inputs(evaluation_input_dir, evaluation_specs)
    images = file_io.load_images(input_dir)
    manifest_path = demo_dir / "replay-manifest.json"
    _write_json(manifest_path, _manifest_document(images))
    failures = _capture_failure_evidence(
        repository=repository,
        demo_dir=demo_dir,
        input_dir=input_dir,
        images=images,
    )

    help_output = _run(repository, ["--help"])
    dry_run_output = _run(repository, [str(input_dir), "--dry-run"])
    replay_output = _run(
        repository,
        [
            str(input_dir),
            "--replay",
            str(manifest_path),
            "--stdout",
        ],
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "help.txt").write_text(help_output, encoding="utf-8")
    (evidence_dir / "dry-run.json").write_text(dry_run_output, encoding="ascii")
    (evidence_dir / "replay-result.json").write_text(
        replay_output,
        encoding="ascii",
    )
    _write_json(evidence_dir / "failure-paths.json", failures)
    provenance_capture = _capture_provenance_evidence(
        repository=repository,
        demo_dir=demo_dir,
        evidence_dir=evidence_dir,
    )
    evaluation_capture = _capture_evaluation_evidence(
        repository=repository,
        demo_dir=demo_dir,
        input_dir=evaluation_input_dir,
        evidence_dir=evidence_dir,
        specs=evaluation_specs,
    )

    coverage = _coverage_summary(
        coverage_path,
        test_count=_collect_test_count(repository),
    )
    _write_json(evidence_dir / "coverage-summary.json", coverage)
    _save_assets(
        asset_dir=asset_dir,
        input_dir=input_dir,
        evaluation_input_dir=evaluation_input_dir,
        help_output=help_output,
        dry_run_output=dry_run_output,
        replay_output=replay_output,
        coverage=coverage,
        failures=failures,
        provenance_capture=provenance_capture,
        evaluation_capture=evaluation_capture,
    )
    _finalize_provenance_source(
        repository=repository,
        output_root=output_root,
        source=provenance_capture["source"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="root receiving demo/ and docs/assets/",
    )
    parser.add_argument(
        "--coverage-json",
        type=Path,
        required=True,
        help="coverage.py JSON from the current full test run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    capture(
        repository=repository,
        output_root=args.output_root.resolve(),
        coverage_path=args.coverage_json.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
