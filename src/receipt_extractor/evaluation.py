"""Content-addressed contracts for synthetic evaluator calibration."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from enum import StrEnum
from itertools import islice
from typing import Annotated, Any, Final, Literal, NoReturn, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from receipt_extractor.file_io import MAX_DIRECTORY_ENTRIES
from receipt_extractor.provenance import receipt_contract_digest
from receipt_extractor.replay import ReplayInputDescriptor, batch_digest
from receipt_extractor.schema import ExpenseCategory, ReceiptFields

SUITE_KIND: Final = "receipt-extractor-evaluation-suite"
REPORT_KIND: Final = "receipt-extractor-evaluation-report"
EVALUATOR_ID: Final = "receipt-extractor/exact-field-evaluator"
_FieldName = Literal["date", "amount", "vendor", "category"]
FIELD_ORDER: Final[tuple[_FieldName, ...]] = (
    "date",
    "amount",
    "vendor",
    "category",
)
CATEGORY_LABELS: Final = (
    "<null>",
    "Meals",
    "Transport",
    "Lodging",
    "Office Supplies",
    "Entertainment",
    "Other",
)

MAX_EVALUATION_BYTES: Final = 1024 * 1024
MAX_EVALUATION_CASES: Final = min(100, MAX_DIRECTORY_ENTRIES)

_SUITE_DOMAIN_V1 = b"auditable-receipt-extractor/evaluation-suite/v1\0"
_REPORT_DOMAIN_V1 = b"auditable-receipt-extractor/evaluation-report/v1\0"
_EVALUATOR_CONTRACT_DOMAIN_V1 = b"auditable-receipt-extractor/evaluator-contract/v1\0"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
_CaseId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]
_SuiteName = Annotated[
    str,
    Field(min_length=1, max_length=96, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


def _require_schema_version_one(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("schema version must be the integer 1")
    return value


_SchemaVersionV1 = Annotated[Literal[1], BeforeValidator(_require_schema_version_one)]


def _require_false_boolean(value: object) -> object:
    if value is not False:
        raise ValueError("live model output must be the boolean false")
    return value


_LiteralFalse = Annotated[Literal[False], BeforeValidator(_require_false_boolean)]
_NonNegativeInt = Annotated[int, Field(ge=0)]
_CaseCount = Annotated[int, Field(ge=1, le=MAX_EVALUATION_CASES)]
_ConfusionRow = Annotated[
    tuple[_NonNegativeInt, ...],
    Field(min_length=len(CATEGORY_LABELS), max_length=len(CATEGORY_LABELS)),
]


class EvaluationError(ValueError):
    """Raised when an evaluation contract is malformed or inconsistent."""


def _fail(message: str) -> NoReturn:
    raise EvaluationError(message)


class FieldOutcome(StrEnum):
    """Exact exhaustive outcome taxonomy for one nullable field."""

    EXACT = "exact"
    OMISSION = "omission"
    SPURIOUS = "spurious"
    SUBSTITUTION = "substitution"


OUTCOME_ORDER: Final = tuple(item.value for item in FieldOutcome)

_EVALUATOR_CONTRACT_DOCUMENT_V1: Final[dict[str, object]] = {
    "id": EVALUATOR_ID,
    "schema_version": 1,
    "comparison_boundary": "validated-receipt-fields",
    "receipt_fields_contract": {
        "id": "receipt-extractor/receipt-fields",
        "schema_version": 1,
        "digest": receipt_contract_digest(),
    },
    "field_order": list(FIELD_ORDER),
    "outcome_order": list(OUTCOME_ORDER),
    "outcomes": {
        "exact": "reference equals candidate, including null equals null",
        "omission": "reference is non-null and candidate is null",
        "spurious": "reference is null and candidate is non-null",
        "substitution": "reference and candidate are non-null and unequal",
    },
    "category_labels": list(CATEGORY_LABELS),
}


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise EvaluationError("the evaluation value is not canonical JSON") from error


_EVALUATOR_CONTRACT_CANONICAL_V1 = _canonical_json_bytes(
    _EVALUATOR_CONTRACT_DOCUMENT_V1
)
_EVALUATOR_CONTRACT_DIGEST_V1: Final = (
    "sha256:0e7fd754958dce9f3e426b8988716296240e15e7cd98588c394d57675f5f55e2"
)

if len(_EVALUATOR_CONTRACT_CANONICAL_V1) != 760:  # pragma: no cover
    raise RuntimeError("evaluator contract v1 canonical length changed")
if (
    "sha256:"
    + hashlib.sha256(
        _EVALUATOR_CONTRACT_DOMAIN_V1 + _EVALUATOR_CONTRACT_CANONICAL_V1
    ).hexdigest()
    != _EVALUATOR_CONTRACT_DIGEST_V1
):  # pragma: no cover
    raise RuntimeError("evaluator contract v1 identity changed")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )


def _validated_model[ModelT: BaseModel](
    value: object,
    model_type: type[ModelT],
    *,
    label: str,
) -> ModelT:
    if not isinstance(value, model_type):
        _fail(f"{label} does not match schema v1")
    try:
        encoded = _canonical_json_bytes(value.model_dump(mode="json"))
        return model_type.model_validate_json(encoded, strict=True)
    except (EvaluationError, RecursionError, UnicodeError, ValueError) as error:
        raise EvaluationError(f"{label} does not match schema v1") from error


class EvaluatorContractIdentity(_StrictModel):
    """Pinned identity of the exact comparison semantics."""

    id: Literal["receipt-extractor/exact-field-evaluator"]
    schema_version: _SchemaVersionV1
    digest: _Digest


class NegativeControlIdentity(_StrictModel):
    """Scope the candidate to an authored calibration control."""

    id: Literal["authored-negative-control/v1"]
    origin: Literal["repository-authored-deliberately-imperfect"]
    live_model_output: _LiteralFalse


class EvaluationCase(_StrictModel):
    """One synthetic input identity and its authored comparison pair."""

    case_id: _CaseId
    input: ReplayInputDescriptor
    truth: ReceiptFields
    candidate: ReceiptFields

    @model_validator(mode="after")
    def validate_input_name(self) -> Self:
        name = self.input.name
        if name in {".", ".."} or any(mark in name for mark in ("/", "\\", ":")):
            raise ValueError("evaluation input name must be a direct-child name")
        return self


class EvaluationSuiteBody(_StrictModel):
    """Semantic body covered by the suite identity."""

    name: _SuiteName
    truth_origin: Literal["repository-authored-synthetic"]
    evaluator: EvaluatorContractIdentity
    candidate: NegativeControlIdentity
    input_batch_digest: _Digest
    cases: Annotated[
        tuple[EvaluationCase, ...],
        Field(min_length=1, max_length=MAX_EVALUATION_CASES),
    ]

    @model_validator(mode="after")
    def validate_case_set(self) -> Self:
        if self.evaluator != evaluator_contract_identity():
            raise ValueError("evaluation suite evaluator contract does not match v1")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        input_names = [case.input.name for case in self.cases]
        if len(input_names) != len(set(input_names)):
            raise ValueError("evaluation input names must be unique")
        expected_digest = batch_digest([case.input for case in self.cases])
        if not hmac.compare_digest(self.input_batch_digest, expected_digest):
            raise ValueError("evaluation input batch digest does not match its cases")
        return self


def evaluator_contract_digest() -> str:
    """Return the pinned evaluator-semantics identity."""

    return (
        "sha256:"
        + hashlib.sha256(
            _EVALUATOR_CONTRACT_DOMAIN_V1 + _EVALUATOR_CONTRACT_CANONICAL_V1
        ).hexdigest()
    )


def evaluator_contract_identity() -> EvaluatorContractIdentity:
    """Build the strict public identity for evaluator contract v1."""

    return EvaluatorContractIdentity(
        id=EVALUATOR_ID,
        schema_version=1,
        digest=evaluator_contract_digest(),
    )


def suite_id_for(body: EvaluationSuiteBody) -> str:
    """Hash one validated suite body with a dedicated domain separator."""

    validated = _validated_model(
        body,
        EvaluationSuiteBody,
        label="the evaluation suite body",
    )
    payload = {
        "kind": SUITE_KIND,
        "schema_version": 1,
        "body": validated.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        _SUITE_DOMAIN_V1 + _canonical_json_bytes(payload)
    ).hexdigest()
    return f"sha256:{digest}"


class EvaluationSuite(_StrictModel):
    """Versioned content-addressed synthetic calibration suite."""

    kind: Literal["receipt-extractor-evaluation-suite"]
    schema_version: _SchemaVersionV1
    suite_id: _Digest
    body: EvaluationSuiteBody

    @model_validator(mode="after")
    def validate_suite_id(self) -> Self:
        if not hmac.compare_digest(self.suite_id, suite_id_for(self.body)):
            raise ValueError("evaluation suite ID does not match its body")
        return self


def _bounded_cases(cases: Sequence[EvaluationCase]) -> tuple[EvaluationCase, ...]:
    try:
        selected = tuple(islice(iter(cases), MAX_EVALUATION_CASES + 1))
    except Exception as error:
        raise EvaluationError("evaluation cases cannot be consumed safely") from error
    if len(selected) > MAX_EVALUATION_CASES:
        _fail("evaluation cases exceed the suite limit")
    if not selected:
        _fail("evaluation suite must contain at least one case")
    return selected


def build_evaluation_suite(
    *,
    name: str,
    cases: Sequence[EvaluationCase],
) -> EvaluationSuite:
    """Build and revalidate one authored synthetic negative-control suite."""

    selected = _bounded_cases(cases)
    try:
        validated_cases = tuple(
            _validated_model(
                case,
                EvaluationCase,
                label="an evaluation case",
            )
            for case in selected
        )
        body = EvaluationSuiteBody(
            name=name,
            truth_origin="repository-authored-synthetic",
            evaluator=evaluator_contract_identity(),
            candidate=NegativeControlIdentity(
                id="authored-negative-control/v1",
                origin="repository-authored-deliberately-imperfect",
                live_model_output=False,
            ),
            input_batch_digest=batch_digest([case.input for case in validated_cases]),
            cases=validated_cases,
        )
        suite = EvaluationSuite(
            kind=SUITE_KIND,
            schema_version=1,
            suite_id=suite_id_for(body),
            body=body,
        )
        return EvaluationSuite.model_validate_json(
            _canonical_json_bytes(suite.model_dump(mode="json")),
            strict=True,
        )
    except (EvaluationError, RecursionError, UnicodeError, ValueError) as error:
        raise EvaluationError("could not build evaluation suite schema v1") from error


def evaluation_suite_json(suite: EvaluationSuite) -> str:
    """Render a stable reviewable suite document with no machine metadata."""

    try:
        validated = EvaluationSuite.model_validate_json(
            _canonical_json_bytes(suite.model_dump(mode="json")),
            strict=True,
        )
        return (
            json.dumps(
                validated.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (RecursionError, UnicodeError, ValueError, ValidationError) as error:
        raise EvaluationError(
            "could not serialize evaluation suite schema v1"
        ) from error


class OutcomeCounts(_StrictModel):
    """Aggregate counts for the exhaustive field-outcome taxonomy."""

    exact: _NonNegativeInt
    omission: _NonNegativeInt
    spurious: _NonNegativeInt
    substitution: _NonNegativeInt

    @property
    def total(self) -> int:
        """Return the derived denominator without serializing it twice."""

        return self.exact + self.omission + self.spurious + self.substitution


class FieldOutcomeCounts(_StrictModel):
    """Outcome counts for one field in the pinned field order."""

    field: _FieldName
    outcomes: OutcomeCounts


class CategoryConfusion(_StrictModel):
    """Fixed-label category confusion matrix with truth on rows."""

    labels: Annotated[
        tuple[str, ...],
        Field(min_length=len(CATEGORY_LABELS), max_length=len(CATEGORY_LABELS)),
    ]
    matrix: Annotated[
        tuple[_ConfusionRow, ...],
        Field(min_length=len(CATEGORY_LABELS), max_length=len(CATEGORY_LABELS)),
    ]

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if self.labels != CATEGORY_LABELS:
            raise ValueError("category confusion labels do not match evaluator v1")
        return self


def _exact_assignment_is_feasible(
    field_degrees: Sequence[int],
    histogram: Sequence[int],
) -> bool:
    """Apply Gale-Ryser to field and anonymous-record exactness degrees."""

    ordered_fields = sorted(field_degrees, reverse=True)
    record_degrees = tuple(
        exact_fields
        for exact_fields, record_count in enumerate(histogram)
        for _ in range(record_count)
    )
    if sum(ordered_fields) != sum(record_degrees):
        return False
    return all(
        sum(ordered_fields[:field_count])
        <= sum(min(field_count, degree) for degree in record_degrees)
        for field_count in range(1, len(ordered_fields) + 1)
    )


class EvaluationMetrics(_StrictModel):
    """Reconciled aggregates without case-level free-text, date, or amount values."""

    case_count: _CaseCount
    per_field: Annotated[
        tuple[FieldOutcomeCounts, ...],
        Field(min_length=len(FIELD_ORDER), max_length=len(FIELD_ORDER)),
    ]
    all_fields: OutcomeCounts
    record_exact_field_histogram: Annotated[
        tuple[_NonNegativeInt, ...],
        Field(min_length=len(FIELD_ORDER) + 1, max_length=len(FIELD_ORDER) + 1),
    ]
    category_confusion: CategoryConfusion

    @model_validator(mode="after")
    def reconcile_aggregates(self) -> Self:
        if tuple(item.field for item in self.per_field) != FIELD_ORDER:
            raise ValueError("per-field metrics do not match evaluator field order")
        if any(item.outcomes.total != self.case_count for item in self.per_field):
            raise ValueError("each field outcome total must equal the case count")

        expected_all = OutcomeCounts(
            exact=sum(item.outcomes.exact for item in self.per_field),
            omission=sum(item.outcomes.omission for item in self.per_field),
            spurious=sum(item.outcomes.spurious for item in self.per_field),
            substitution=sum(item.outcomes.substitution for item in self.per_field),
        )
        if self.all_fields != expected_all:
            raise ValueError("all-field outcomes do not equal the per-field sum")
        if self.all_fields.total != self.case_count * len(FIELD_ORDER):
            raise ValueError("all-field outcome total does not match the case count")

        histogram = self.record_exact_field_histogram
        if sum(histogram) != self.case_count:
            raise ValueError("record histogram total must equal the case count")
        weighted_exact = sum(index * count for index, count in enumerate(histogram))
        if weighted_exact != self.all_fields.exact:
            raise ValueError("record histogram does not reconcile exact fields")
        if not _exact_assignment_is_feasible(
            [item.outcomes.exact for item in self.per_field],
            histogram,
        ):
            raise ValueError(
                "field exact counts and record histogram are not jointly feasible"
            )

        matrix = self.category_confusion.matrix
        if sum(sum(row) for row in matrix) != self.case_count:
            raise ValueError("category confusion total must equal the case count")
        category = self.per_field[FIELD_ORDER.index("category")].outcomes
        category_exact = sum(matrix[index][index] for index in range(len(matrix)))
        category_omission = sum(row[0] for row in matrix[1:])
        category_spurious = sum(matrix[0][1:])
        category_substitution = sum(
            matrix[row][column]
            for row in range(1, len(matrix))
            for column in range(1, len(matrix))
            if row != column
        )
        if (
            category_exact,
            category_omission,
            category_spurious,
            category_substitution,
        ) != (
            category.exact,
            category.omission,
            category.spurious,
            category.substitution,
        ):
            raise ValueError("category confusion does not reconcile field outcomes")
        return self


class EvaluationReportBody(_StrictModel):
    """Suite and contract bindings covered by one aggregate report identity."""

    mode: Literal["synthetic-negative-control-calibration"]
    evaluator: EvaluatorContractIdentity
    truth_origin: Literal["repository-authored-synthetic"]
    candidate: NegativeControlIdentity
    suite_id: _Digest
    input_batch_digest: _Digest
    metrics: EvaluationMetrics

    @model_validator(mode="after")
    def validate_evaluator(self) -> Self:
        if self.evaluator != evaluator_contract_identity():
            raise ValueError("evaluation report evaluator contract does not match v1")
        return self


def report_id_for(body: EvaluationReportBody) -> str:
    """Hash one validated aggregate report body with its own domain."""

    validated = _validated_model(
        body,
        EvaluationReportBody,
        label="the evaluation report body",
    )
    payload = {
        "kind": REPORT_KIND,
        "schema_version": 1,
        "body": validated.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        _REPORT_DOMAIN_V1 + _canonical_json_bytes(payload)
    ).hexdigest()
    return f"sha256:{digest}"


class EvaluationReport(_StrictModel):
    """Content-addressed aggregate receipt for one calibrated suite."""

    kind: Literal["receipt-extractor-evaluation-report"]
    schema_version: _SchemaVersionV1
    report_id: _Digest
    body: EvaluationReportBody

    @model_validator(mode="after")
    def validate_report_id(self) -> Self:
        if not hmac.compare_digest(self.report_id, report_id_for(self.body)):
            raise ValueError("evaluation report ID does not match its body")
        return self


def _field_outcome(reference: object, candidate: object) -> FieldOutcome:
    if reference == candidate:
        return FieldOutcome.EXACT
    if reference is None:
        return FieldOutcome.SPURIOUS
    if candidate is None:
        return FieldOutcome.OMISSION
    return FieldOutcome.SUBSTITUTION


def _receipt_values(
    receipt: ReceiptFields,
) -> tuple[str | ExpenseCategory | None, ...]:
    return (
        receipt.date,
        receipt.amount,
        receipt.vendor,
        receipt.category,
    )


def receipt_field_outcomes(
    truth: ReceiptFields,
    candidate: ReceiptFields,
) -> tuple[FieldOutcome, ...]:
    """Classify exact outcomes after the shared ReceiptFields boundary."""

    validated_truth = _validated_model(
        truth,
        ReceiptFields,
        label="the evaluation truth",
    )
    validated_candidate = _validated_model(
        candidate,
        ReceiptFields,
        label="the evaluation candidate",
    )
    return tuple(
        _field_outcome(reference, observed)
        for reference, observed in zip(
            _receipt_values(validated_truth),
            _receipt_values(validated_candidate),
            strict=True,
        )
    )


def _category_index(value: ExpenseCategory | None) -> int:
    label = CATEGORY_LABELS[0] if value is None else value.value
    return CATEGORY_LABELS.index(label)


def _outcome_counts(values: dict[FieldOutcome, int]) -> OutcomeCounts:
    return OutcomeCounts(
        exact=values[FieldOutcome.EXACT],
        omission=values[FieldOutcome.OMISSION],
        spurious=values[FieldOutcome.SPURIOUS],
        substitution=values[FieldOutcome.SUBSTITUTION],
    )


def evaluate_suite(suite: EvaluationSuite) -> EvaluationReport:
    """Evaluate one authored control without a provider, network, or clock."""

    validated_suite = _validated_model(
        suite,
        EvaluationSuite,
        label="the evaluation suite",
    )
    field_counts = {
        field: {outcome: 0 for outcome in FieldOutcome} for field in FIELD_ORDER
    }
    record_histogram = [0] * (len(FIELD_ORDER) + 1)
    category_matrix = [[0 for _ in CATEGORY_LABELS] for _ in CATEGORY_LABELS]

    for case in validated_suite.body.cases:
        outcomes = receipt_field_outcomes(case.truth, case.candidate)
        record_histogram[outcomes.count(FieldOutcome.EXACT)] += 1
        for field, outcome in zip(FIELD_ORDER, outcomes, strict=True):
            field_counts[field][outcome] += 1
        truth_index = _category_index(case.truth.category)
        candidate_index = _category_index(case.candidate.category)
        category_matrix[truth_index][candidate_index] += 1

    per_field = tuple(
        FieldOutcomeCounts(field=field, outcomes=_outcome_counts(field_counts[field]))
        for field in FIELD_ORDER
    )
    all_fields = OutcomeCounts(
        exact=sum(item.outcomes.exact for item in per_field),
        omission=sum(item.outcomes.omission for item in per_field),
        spurious=sum(item.outcomes.spurious for item in per_field),
        substitution=sum(item.outcomes.substitution for item in per_field),
    )
    try:
        metrics = EvaluationMetrics(
            case_count=len(validated_suite.body.cases),
            per_field=per_field,
            all_fields=all_fields,
            record_exact_field_histogram=tuple(record_histogram),
            category_confusion=CategoryConfusion(
                labels=CATEGORY_LABELS,
                matrix=tuple(tuple(row) for row in category_matrix),
            ),
        )
        body = EvaluationReportBody(
            mode="synthetic-negative-control-calibration",
            evaluator=validated_suite.body.evaluator,
            truth_origin=validated_suite.body.truth_origin,
            candidate=validated_suite.body.candidate,
            suite_id=validated_suite.suite_id,
            input_batch_digest=validated_suite.body.input_batch_digest,
            metrics=metrics,
        )
        report = EvaluationReport(
            kind=REPORT_KIND,
            schema_version=1,
            report_id=report_id_for(body),
            body=body,
        )
        return _validated_model(
            report,
            EvaluationReport,
            label="the evaluation report",
        )
    except (EvaluationError, RecursionError, UnicodeError, ValueError) as error:
        raise EvaluationError("could not evaluate suite with contract v1") from error


def evaluation_report_json(report: EvaluationReport) -> str:
    """Render a stable aggregate receipt without case-level values or names."""

    try:
        validated = _validated_model(
            report,
            EvaluationReport,
            label="the evaluation report",
        )
        return (
            json.dumps(
                validated.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (EvaluationError, RecursionError, UnicodeError, ValueError) as error:
        raise EvaluationError(
            "could not serialize evaluation report schema v1"
        ) from error
