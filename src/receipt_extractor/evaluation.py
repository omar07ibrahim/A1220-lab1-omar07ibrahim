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
from receipt_extractor.schema import ReceiptFields

SUITE_KIND: Final = "receipt-extractor-evaluation-suite"
EVALUATOR_ID: Final = "receipt-extractor/exact-field-evaluator"
FIELD_ORDER: Final = ("date", "amount", "vendor", "category")
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
