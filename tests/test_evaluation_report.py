from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from itertools import product
from typing import cast

import pytest
from pydantic import ValidationError

from receipt_extractor.evaluation import (
    CATEGORY_LABELS,
    FIELD_ORDER,
    CategoryConfusion,
    EvaluationCase,
    EvaluationError,
    EvaluationMetrics,
    EvaluationReport,
    EvaluationReportBody,
    FieldOutcome,
    FieldOutcomeCounts,
    OutcomeCounts,
    build_evaluation_suite,
    evaluate_suite,
    evaluation_report_json,
    evaluator_contract_identity,
    receipt_field_outcomes,
    report_id_for,
)
from receipt_extractor.file_io import ImagePayload
from receipt_extractor.replay import descriptor_for
from receipt_extractor.schema import ExpenseCategory, ReceiptFields


def _receipt(
    *,
    date: str | None,
    amount: str | None,
    vendor: str | None,
    category: str | None,
) -> ReceiptFields:
    return ReceiptFields.model_validate_json(
        json.dumps(
            {
                "date": date,
                "amount": amount,
                "vendor": vendor,
                "category": category,
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        strict=True,
    )


def _case(
    case_id: str,
    *,
    truth: ReceiptFields,
    candidate: ReceiptFields,
) -> EvaluationCase:
    data = f"synthetic-evaluation:{case_id}".encode("ascii")
    image = ImagePayload(
        name=f"{case_id}.png",
        media_type="image/png",
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=16,
        height=12,
    )
    return EvaluationCase(
        case_id=case_id,
        input=descriptor_for(image),
        truth=truth,
        candidate=candidate,
    )


def _balanced_cases() -> tuple[EvaluationCase, ...]:
    values = (
        (
            "exact-cafe",
            _receipt(
                date="2026-07-01",
                amount="$12.40",
                vendor="Northstar Cafe",
                category="Meals",
            ),
            _receipt(
                date="2026-07-01",
                amount="$12.40",
                vendor="Northstar Cafe",
                category="Meals",
            ),
        ),
        (
            "metro-amount-category",
            _receipt(
                date="2026-07-02",
                amount="$4.50",
                vendor="Metro Transit",
                category="Transport",
            ),
            _receipt(
                date="2026-07-02",
                amount="$5.40",
                vendor="Metro Transit",
                category=None,
            ),
        ),
        (
            "hotel-date-vendor",
            _receipt(
                date="2026-07-03",
                amount="$148.00",
                vendor="Atlas Hotel",
                category="Lodging",
            ),
            _receipt(
                date=None,
                amount="$148.00",
                vendor="Atlas Motel",
                category="Lodging",
            ),
        ),
        (
            "office-date-amount",
            _receipt(
                date="2026-07-04",
                amount="$23.90",
                vendor="Paper & Pine",
                category="Office Supplies",
            ),
            _receipt(
                date="2026-04-07",
                amount=None,
                vendor="Paper & Pine",
                category="Office Supplies",
            ),
        ),
        (
            "cinema-vendor-category",
            _receipt(
                date="2026-07-05",
                amount="$18.00",
                vendor="Beacon Cinema",
                category="Entertainment",
            ),
            _receipt(
                date="2026-07-05",
                amount="$18.00",
                vendor=None,
                category="Meals",
            ),
        ),
        (
            "exact-kiosk",
            _receipt(
                date="2026-07-06",
                amount="$7.25",
                vendor="Civic Kiosk",
                category="Other",
            ),
            _receipt(
                date="2026-07-06",
                amount="$7.25",
                vendor="Civic Kiosk",
                category="Other",
            ),
        ),
        (
            "null-date-vendor",
            _receipt(
                date=None,
                amount="$3.00",
                vendor=None,
                category="Other",
            ),
            _receipt(
                date="2026-07-07",
                amount="$3.00",
                vendor="Ghost Vendor",
                category="Other",
            ),
        ),
        (
            "null-amount-category",
            _receipt(
                date="2026-07-08",
                amount=None,
                vendor="Null Market",
                category=None,
            ),
            _receipt(
                date="2026-07-08",
                amount="$9.99",
                vendor="Null Market",
                category="Other",
            ),
        ),
    )
    return tuple(
        _case(case_id, truth=truth, candidate=candidate)
        for case_id, truth, candidate in values
    )


def _balanced_report() -> EvaluationReport:
    suite = build_evaluation_suite(
        name="balanced-field-outcomes-v1",
        cases=_balanced_cases(),
    )
    return evaluate_suite(suite)


def _json_scalars(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _json_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_scalars(item)
    else:
        yield value


@pytest.mark.parametrize(
    ("truth_vendor", "candidate_vendor", "expected"),
    (
        (None, None, FieldOutcome.EXACT),
        ("Same", "Same", FieldOutcome.EXACT),
        ("Missing", None, FieldOutcome.OMISSION),
        (None, "Invented", FieldOutcome.SPURIOUS),
        ("Expected", "Observed", FieldOutcome.SUBSTITUTION),
    ),
)
def test_field_outcome_truth_table_is_exhaustive(
    truth_vendor: str | None,
    candidate_vendor: str | None,
    expected: FieldOutcome,
) -> None:
    truth = _receipt(
        date="2026-07-01",
        amount="$1.00",
        vendor=truth_vendor,
        category="Other",
    )
    candidate = _receipt(
        date="2026-07-01",
        amount="$1.00",
        vendor=candidate_vendor,
        category="Other",
    )

    outcomes = receipt_field_outcomes(truth, candidate)

    assert outcomes == (
        FieldOutcome.EXACT,
        FieldOutcome.EXACT,
        expected,
        FieldOutcome.EXACT,
    )


def test_comparison_uses_validated_values_but_no_domain_normalization() -> None:
    truth = _receipt(
        date="2026-07-01",
        amount="$12.00",
        vendor="  Northstar Cafe  ",
        category="Meals",
    )
    candidate = _receipt(
        date="07/01/2026",
        amount="USD 12.00",
        vendor="Northstar Cafe",
        category="Meals",
    )

    assert receipt_field_outcomes(truth, candidate) == (
        FieldOutcome.SUBSTITUTION,
        FieldOutcome.SUBSTITUTION,
        FieldOutcome.EXACT,
        FieldOutcome.EXACT,
    )


def test_balanced_negative_control_reconciles_every_aggregate() -> None:
    report = _balanced_report()
    metrics = report.body.metrics
    balanced = OutcomeCounts(exact=5, omission=1, spurious=1, substitution=1)

    assert report.kind == "receipt-extractor-evaluation-report"
    assert report.schema_version == 1
    assert report.report_id == report_id_for(report.body)
    assert report.body.evaluator == evaluator_contract_identity()
    assert metrics.case_count == 8
    assert tuple(item.field for item in metrics.per_field) == FIELD_ORDER
    assert all(item.outcomes == balanced for item in metrics.per_field)
    assert metrics.all_fields == OutcomeCounts(
        exact=20,
        omission=4,
        spurious=4,
        substitution=4,
    )
    assert metrics.record_exact_field_histogram == (0, 0, 6, 0, 2)
    assert metrics.category_confusion.labels == CATEGORY_LABELS
    assert metrics.category_confusion.matrix == (
        (0, 0, 0, 0, 0, 0, 1),
        (0, 1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 1, 0, 0, 0),
        (0, 0, 0, 0, 1, 0, 0),
        (0, 1, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 2),
    )
    assert report.report_id == (
        "sha256:71064ffb8c41c1196faf4382dfb6c8cb60cfbce10122b2e4012c3472666d329c"
    )


def test_report_is_canonical_aggregate_only_and_byte_deterministic() -> None:
    first = _balanced_report()
    second = _balanced_report()
    text = evaluation_report_json(first)
    decoded = json.loads(text)

    assert first == second
    assert text == evaluation_report_json(second)
    assert text == (
        json.dumps(decoded, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )
    assert set(decoded["body"]) == {
        "candidate",
        "evaluator",
        "input_batch_digest",
        "metrics",
        "mode",
        "suite_id",
        "truth_origin",
    }
    assert set(decoded["body"]["metrics"]) == {
        "all_fields",
        "case_count",
        "category_confusion",
        "per_field",
        "record_exact_field_histogram",
    }
    for case in _balanced_cases():
        assert case.input.name not in text
        for value in (case.truth.date, case.truth.amount, case.truth.vendor):
            if value is not None:
                assert value not in text
        for value in (
            case.candidate.date,
            case.candidate.amount,
            case.candidate.vendor,
        ):
            if value is not None:
                assert value not in text
    assert not any(isinstance(value, float) for value in _json_scalars(decoded))


def test_report_identity_binds_suite_order_and_complete_body() -> None:
    cases = _balanced_cases()
    first_suite = build_evaluation_suite(
        name="balanced-field-outcomes-v1",
        cases=cases,
    )
    reversed_suite = build_evaluation_suite(
        name="balanced-field-outcomes-v1",
        cases=tuple(reversed(cases)),
    )
    first = evaluate_suite(first_suite)
    reversed_report = evaluate_suite(reversed_suite)
    changed_body = first.body.model_copy(update={"suite_id": "sha256:" + "0" * 64})

    assert first.body.metrics == reversed_report.body.metrics
    assert first_suite.suite_id != reversed_suite.suite_id
    assert first.body.input_batch_digest != reversed_report.body.input_batch_digest
    assert first.report_id != reversed_report.report_id
    assert report_id_for(changed_body) != first.report_id


def test_public_report_apis_revalidate_model_copy_corruption() -> None:
    report = _balanced_report()
    bad_evaluator = report.body.evaluator.model_copy(
        update={"digest": "sha256:" + "0" * 64}
    )
    bad_body = report.body.model_copy(update={"evaluator": bad_evaluator})
    bad_report = report.model_copy(update={"report_id": "sha256:" + "0" * 64})
    suite = build_evaluation_suite(
        name="balanced-field-outcomes-v1",
        cases=_balanced_cases(),
    )
    bad_suite = suite.model_copy(update={"suite_id": "sha256:" + "0" * 64})

    with pytest.raises(EvaluationError, match=r"report body.*schema v1"):
        report_id_for(bad_body)
    with pytest.raises(EvaluationError, match="could not serialize"):
        evaluation_report_json(bad_report)
    with pytest.raises(EvaluationError, match=r"evaluation suite.*schema v1"):
        evaluate_suite(bad_suite)
    with pytest.raises(EvaluationError, match=r"evaluation truth.*schema v1"):
        receipt_field_outcomes(
            cast(ReceiptFields, object()),
            _balanced_cases()[0].candidate,
        )


def test_metric_validators_reject_field_aggregate_and_histogram_drift() -> None:
    document = _balanced_report().body.metrics.model_dump(mode="json")
    document["per_field"][0], document["per_field"][1] = (
        document["per_field"][1],
        document["per_field"][0],
    )
    with pytest.raises(ValidationError, match="field order"):
        EvaluationMetrics.model_validate_json(json.dumps(document), strict=True)

    document = _balanced_report().body.metrics.model_dump(mode="json")
    document["per_field"][0]["outcomes"]["exact"] += 1
    with pytest.raises(ValidationError, match="field outcome total"):
        EvaluationMetrics.model_validate_json(json.dumps(document), strict=True)

    document = _balanced_report().body.metrics.model_dump(mode="json")
    document["all_fields"]["exact"] += 1
    with pytest.raises(ValidationError, match="per-field sum"):
        EvaluationMetrics.model_validate_json(json.dumps(document), strict=True)

    document = _balanced_report().body.metrics.model_dump(mode="json")
    document["record_exact_field_histogram"][0] += 1
    with pytest.raises(ValidationError, match="histogram total"):
        EvaluationMetrics.model_validate_json(json.dumps(document), strict=True)

    document = _balanced_report().body.metrics.model_dump(mode="json")
    document["record_exact_field_histogram"][1] += 1
    document["record_exact_field_histogram"][2] -= 1
    with pytest.raises(ValidationError, match="reconcile exact fields"):
        EvaluationMetrics.model_validate_json(json.dumps(document), strict=True)


@pytest.mark.parametrize(
    ("case_count", "field_exact", "histogram"),
    (
        (2, (2, 2, 0, 0), (1, 0, 0, 0, 1)),
        (3, (3, 3, 3, 0), (0, 1, 0, 0, 2)),
    ),
)
def test_metrics_reject_arithmetically_balanced_but_impossible_exactness(
    case_count: int,
    field_exact: tuple[int, int, int, int],
    histogram: tuple[int, int, int, int, int],
) -> None:
    outcomes = tuple(
        OutcomeCounts(
            exact=exact,
            omission=0,
            spurious=0,
            substitution=case_count - exact,
        )
        for exact in field_exact
    )
    matrix = [[0 for _ in CATEGORY_LABELS] for _ in CATEGORY_LABELS]
    matrix[1][2] = case_count

    with pytest.raises(ValidationError, match="not jointly feasible"):
        EvaluationMetrics(
            case_count=case_count,
            per_field=(
                FieldOutcomeCounts(field="date", outcomes=outcomes[0]),
                FieldOutcomeCounts(field="amount", outcomes=outcomes[1]),
                FieldOutcomeCounts(field="vendor", outcomes=outcomes[2]),
                FieldOutcomeCounts(field="category", outcomes=outcomes[3]),
            ),
            all_fields=OutcomeCounts(
                exact=sum(field_exact),
                omission=0,
                spurious=0,
                substitution=case_count * len(FIELD_ORDER) - sum(field_exact),
            ),
            record_exact_field_histogram=histogram,
            category_confusion=CategoryConfusion(
                labels=CATEGORY_LABELS,
                matrix=tuple(tuple(row) for row in matrix),
            ),
        )


def test_category_confusion_rejects_shape_labels_counts_and_reconciliation() -> None:
    report = _balanced_report()
    confusion = report.body.metrics.category_confusion.model_dump(mode="json")
    confusion["labels"][0], confusion["labels"][1] = (
        confusion["labels"][1],
        confusion["labels"][0],
    )
    with pytest.raises(ValidationError, match="labels do not match"):
        CategoryConfusion.model_validate_json(json.dumps(confusion), strict=True)

    confusion = report.body.metrics.category_confusion.model_dump(mode="json")
    confusion["matrix"][0] = confusion["matrix"][0][:-1]
    with pytest.raises(ValidationError):
        CategoryConfusion.model_validate_json(json.dumps(confusion), strict=True)

    confusion = report.body.metrics.category_confusion.model_dump(mode="json")
    confusion["matrix"][0][0] = -1
    with pytest.raises(ValidationError):
        CategoryConfusion.model_validate_json(json.dumps(confusion), strict=True)

    metrics = report.body.metrics.model_dump(mode="json")
    metrics["category_confusion"]["matrix"][1][1] -= 1
    metrics["category_confusion"]["matrix"][1][6] += 1
    with pytest.raises(ValidationError, match="does not reconcile"):
        EvaluationMetrics.model_validate_json(json.dumps(metrics), strict=True)

    metrics = report.body.metrics.model_dump(mode="json")
    metrics["category_confusion"]["matrix"][0][0] += 1
    with pytest.raises(ValidationError, match="confusion total"):
        EvaluationMetrics.model_validate_json(json.dumps(metrics), strict=True)


@pytest.mark.parametrize("value", (True, "5", 5.0, -1))
def test_outcome_counts_require_non_negative_strict_integers(value: object) -> None:
    document = {
        "exact": value,
        "omission": 0,
        "spurious": 0,
        "substitution": 0,
    }

    with pytest.raises(ValidationError):
        OutcomeCounts.model_validate_json(json.dumps(document), strict=True)


_CATEGORY_VALUES = (None, *tuple(item.value for item in ExpenseCategory))


@pytest.mark.parametrize(
    ("truth_category", "candidate_category"),
    tuple(product(_CATEGORY_VALUES, repeat=2)),
)
def test_all_category_pairs_land_in_one_matrix_cell(
    truth_category: str | None,
    candidate_category: str | None,
) -> None:
    truth = _receipt(
        date="2026-07-01",
        amount="$1.00",
        vendor="Category Grid",
        category=truth_category,
    )
    candidate = _receipt(
        date="2026-07-01",
        amount="$1.00",
        vendor="Category Grid",
        category=candidate_category,
    )
    suite = build_evaluation_suite(
        name="category-grid-case",
        cases=(_case("category-grid", truth=truth, candidate=candidate),),
    )

    metrics = evaluate_suite(suite).body.metrics
    matrix = metrics.category_confusion.matrix
    truth_label = "<null>" if truth_category is None else truth_category
    candidate_label = "<null>" if candidate_category is None else candidate_category
    row = CATEGORY_LABELS.index(truth_label)
    column = CATEGORY_LABELS.index(candidate_label)
    expected = (
        FieldOutcome.EXACT
        if truth_category == candidate_category
        else FieldOutcome.SPURIOUS
        if truth_category is None
        else FieldOutcome.OMISSION
        if candidate_category is None
        else FieldOutcome.SUBSTITUTION
    )
    category_counts = metrics.per_field[FIELD_ORDER.index("category")].outcomes

    assert matrix[row][column] == 1
    assert sum(sum(values) for values in matrix) == 1
    assert getattr(category_counts, expected.value) == 1
    assert category_counts.total == 1


def test_report_schema_rejects_tampered_id_version_and_extra_fields() -> None:
    document = json.loads(evaluation_report_json(_balanced_report()))
    document["report_id"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="report ID does not match"):
        EvaluationReport.model_validate_json(json.dumps(document), strict=True)

    document = json.loads(evaluation_report_json(_balanced_report()))
    document["schema_version"] = 1.0
    with pytest.raises(ValidationError, match="integer 1"):
        EvaluationReport.model_validate_json(json.dumps(document), strict=True)

    document = json.loads(evaluation_report_json(_balanced_report()))
    document["body"]["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationReport.model_validate_json(json.dumps(document), strict=True)


def test_report_body_constructor_rejects_unpinned_evaluator() -> None:
    report = _balanced_report()
    evaluator = report.body.evaluator.model_copy(
        update={"digest": "sha256:" + "f" * 64}
    )

    with pytest.raises(ValidationError, match="evaluator contract does not match"):
        EvaluationReportBody(
            mode=report.body.mode,
            evaluator=evaluator,
            truth_origin=report.body.truth_origin,
            candidate=report.body.candidate,
            suite_id=report.body.suite_id,
            input_batch_digest=report.body.input_batch_digest,
            metrics=report.body.metrics,
        )
