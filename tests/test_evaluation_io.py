from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from receipt_extractor.evaluation import (
    MAX_EVALUATION_BYTES,
    REPORT_KIND,
    EvaluationCase,
    EvaluationError,
    EvaluationReport,
    EvaluationReportBody,
    EvaluationSuite,
    build_evaluation_suite,
    evaluate_suite,
    evaluation_report_json,
    evaluation_suite_json,
    load_evaluation_report,
    load_evaluation_suite,
    report_id_for,
    verify_evaluation_report,
)
from receipt_extractor.file_io import ImagePayload
from receipt_extractor.replay import descriptor_for
from receipt_extractor.schema import ReceiptFields


def _receipt(*, vendor: str, category: str = "Other") -> ReceiptFields:
    return ReceiptFields.model_validate_json(
        json.dumps(
            {
                "date": "2026-07-24",
                "amount": "$12.34",
                "vendor": vendor,
                "category": category,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        strict=True,
    )


def _suite(*, candidate_vendor: str = "Synthetic Candidate") -> EvaluationSuite:
    data = b"synthetic evaluation input"
    image = ImagePayload(
        name="synthetic-case.png",
        media_type="image/png",
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=12,
        height=8,
    )
    case = EvaluationCase(
        case_id="synthetic-case",
        input=descriptor_for(image),
        truth=_receipt(vendor="Synthetic Truth"),
        candidate=_receipt(vendor=candidate_vendor),
    )
    return build_evaluation_suite(name="io-contract-v1", cases=(case,))


def _write_bundle(
    tmp_path: Path,
) -> tuple[Path, Path, EvaluationSuite, EvaluationReport]:
    suite = _suite()
    report = evaluate_suite(suite)
    suite_path = tmp_path / "suite.json"
    report_path = tmp_path / "report.json"
    suite_path.write_text(evaluation_suite_json(suite), encoding="ascii")
    report_path.write_text(evaluation_report_json(report), encoding="ascii")
    return suite_path, report_path, suite, report


def test_bounded_loaders_round_trip_and_verifier_recomputes_complete_report(
    tmp_path: Path,
) -> None:
    suite_path, report_path, suite, report = _write_bundle(tmp_path)

    loaded_suite = load_evaluation_suite(suite_path)
    loaded_report = load_evaluation_report(report_path)
    verify_evaluation_report(suite=loaded_suite, report=loaded_report)

    assert loaded_suite == suite
    assert loaded_report == report


def test_semantic_identity_allows_json_whitespace_but_not_body_changes(
    tmp_path: Path,
) -> None:
    suite = _suite()
    compact = tmp_path / "compact-suite.json"
    compact.write_text(
        json.dumps(suite.model_dump(mode="json"), separators=(",", ":")),
        encoding="ascii",
    )

    assert load_evaluation_suite(compact) == suite

    document = suite.model_dump(mode="json")
    document["body"]["cases"][0]["candidate"]["vendor"] = "Changed Candidate"
    compact.write_text(json.dumps(document), encoding="ascii")
    with pytest.raises(EvaluationError, match="does not match schema v1"):
        load_evaluation_suite(compact)


def test_verifier_rejects_a_valid_report_from_a_different_suite() -> None:
    expected_suite = _suite(candidate_vendor="Expected Candidate")
    other_suite = _suite(candidate_vendor="Other Candidate")
    other_report = evaluate_suite(other_suite)

    with pytest.raises(EvaluationError, match="does not match the exact suite"):
        verify_evaluation_report(suite=expected_suite, report=other_report)


def test_verifier_rejects_rehashed_wrong_metrics_with_expected_bindings() -> None:
    expected_suite = _suite(candidate_vendor="Expected Candidate")
    other_suite = _suite(candidate_vendor="Synthetic Truth")
    other_report = evaluate_suite(other_suite)
    forged_body = EvaluationReportBody(
        mode=other_report.body.mode,
        evaluator=other_report.body.evaluator,
        truth_origin=other_report.body.truth_origin,
        candidate=other_report.body.candidate,
        suite_id=expected_suite.suite_id,
        input_batch_digest=expected_suite.body.input_batch_digest,
        metrics=other_report.body.metrics,
    )
    forged_report = EvaluationReport(
        kind=REPORT_KIND,
        schema_version=1,
        report_id=report_id_for(forged_body),
        body=forged_body,
    )

    with pytest.raises(EvaluationError, match="does not match the exact suite"):
        verify_evaluation_report(suite=expected_suite, report=forged_report)


def test_verifier_and_loaders_reject_model_copy_and_wrong_public_types(
    tmp_path: Path,
) -> None:
    suite_path, report_path, suite, report = _write_bundle(tmp_path)
    bad_suite = suite.model_copy(update={"suite_id": "sha256:" + "0" * 64})
    bad_report = report.model_copy(update={"report_id": "sha256:" + "0" * 64})

    with pytest.raises(EvaluationError, match=r"evaluation suite.*schema v1"):
        verify_evaluation_report(suite=bad_suite, report=report)
    with pytest.raises(EvaluationError, match=r"evaluation report.*schema v1"):
        verify_evaluation_report(suite=suite, report=bad_report)
    with pytest.raises(EvaluationError, match="path is invalid"):
        load_evaluation_suite(cast(Path, str(suite_path)))
    with pytest.raises(EvaluationError, match="path is invalid"):
        load_evaluation_report(cast(Path, str(report_path)))


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    (
        ("duplicate", b'{"kind":"a","kind":"b"}', "duplicate JSON key"),
        (
            "nested-duplicate",
            b'{"body":{"case":1,"case":2}}',
            "duplicate JSON key",
        ),
        (
            "escaped-duplicate",
            b'{"body":{"case":1,"\\u0063ase":2}}',
            "duplicate JSON key",
        ),
        ("bom", b"\xef\xbb\xbf{}", "UTF-8 BOM"),
        ("utf8", b'{"kind":"\xff"}', "strict UTF-8"),
        ("nan", b'{"value":NaN}', "non-finite JSON"),
        ("infinity", b'{"value":Infinity}', "non-finite JSON"),
        ("negative-infinity", b'{"value":-Infinity}', "non-finite JSON"),
        ("overflow", b'{"value":1e9999}', "non-finite JSON"),
        ("malformed", b'{"value":', "valid JSON"),
    ),
)
def test_loaders_reject_ambiguous_or_noncanonical_json_tokens(
    tmp_path: Path,
    name: str,
    raw: bytes,
    message: str,
) -> None:
    path = tmp_path / f"{name}.json"
    path.write_bytes(raw)

    with pytest.raises(EvaluationError, match=message):
        load_evaluation_suite(path)
    with pytest.raises(EvaluationError, match=message):
        load_evaluation_report(path)


def test_loaders_reject_symlink_hardlink_fifo_oversize_and_wrong_extension(
    tmp_path: Path,
) -> None:
    suite_path, _, _, _ = _write_bundle(tmp_path)

    symlink = tmp_path / "suite-symlink.json"
    symlink.symlink_to(suite_path)
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(symlink)

    hardlink = tmp_path / "suite-hardlink.json"
    os.link(suite_path, hardlink)
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(hardlink)

    fifo = tmp_path / "suite-fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(fifo)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * MAX_EVALUATION_BYTES)
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(oversized)

    wrong_extension = tmp_path / "suite.txt"
    wrong_extension.write_text("{}", encoding="ascii")
    with pytest.raises(EvaluationError, match=r"name a \.json file"):
        load_evaluation_suite(wrong_extension)

    empty = tmp_path / "empty.json"
    empty.touch()
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(empty)

    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(EvaluationError, match="single-link regular file"):
        load_evaluation_suite(directory)

    control_path = tmp_path / "control\nname.json"
    control_path.write_text("{}", encoding="ascii")
    with pytest.raises(EvaluationError, match="control or format character"):
        load_evaluation_suite(control_path)


def test_loader_accepts_a_valid_document_at_the_exact_size_limit(
    tmp_path: Path,
) -> None:
    suite = _suite()
    encoded = evaluation_suite_json(suite).encode("ascii")
    assert len(encoded) < MAX_EVALUATION_BYTES
    exact = tmp_path / "exact-limit.json"
    exact.write_bytes(encoded + b" " * (MAX_EVALUATION_BYTES - len(encoded)))

    assert exact.stat().st_size == MAX_EVALUATION_BYTES
    assert load_evaluation_suite(exact) == suite


def test_loader_rejects_a_symlinked_parent_directory(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    suite = _suite()
    (real_parent / "suite.json").write_text(
        evaluation_suite_json(suite),
        encoding="ascii",
    )
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(EvaluationError, match="parent directory"):
        load_evaluation_suite(linked_parent / "suite.json")


def test_loaders_reject_parent_traversal_and_crossed_artifact_types(
    tmp_path: Path,
) -> None:
    suite_path, report_path, _, _ = _write_bundle(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    traversal = nested / ".." / suite_path.name

    with pytest.raises(EvaluationError, match="must not traverse"):
        load_evaluation_suite(traversal)
    with pytest.raises(EvaluationError, match="does not match schema v1"):
        load_evaluation_report(suite_path)
    with pytest.raises(EvaluationError, match="does not match schema v1"):
        load_evaluation_suite(report_path)


def test_report_loader_rejects_tampered_identity_and_aggregate(
    tmp_path: Path,
) -> None:
    _, report_path, _, report = _write_bundle(tmp_path)
    document = report.model_dump(mode="json")
    document["body"]["metrics"]["all_fields"]["exact"] += 1
    document["report_id"] = "sha256:" + "0" * 64
    report_path.write_text(json.dumps(document), encoding="ascii")

    with pytest.raises(EvaluationError, match="does not match schema v1"):
        load_evaluation_report(report_path)
