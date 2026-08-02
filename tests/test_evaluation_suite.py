from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, cast, overload

import pytest
from pydantic import ValidationError

from receipt_extractor.evaluation import (
    CATEGORY_LABELS,
    EVALUATOR_ID,
    FIELD_ORDER,
    MAX_EVALUATION_CASES,
    OUTCOME_ORDER,
    EvaluationCase,
    EvaluationError,
    EvaluationSuite,
    EvaluationSuiteBody,
    NegativeControlIdentity,
    build_evaluation_suite,
    evaluation_suite_json,
    evaluator_contract_digest,
    evaluator_contract_identity,
    suite_id_for,
)
from receipt_extractor.file_io import ImagePayload
from receipt_extractor.provenance import receipt_contract_digest
from receipt_extractor.replay import batch_digest, descriptor_for
from receipt_extractor.schema import ExpenseCategory, ReceiptFields


def _receipt(
    *,
    date: str | None = "2026-07-24",
    amount: str | None = "$12.34",
    vendor: str | None = "Synthetic Vendor",
    category: str | None = "Other",
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
    data: bytes | None = None,
    truth: ReceiptFields | None = None,
    candidate: ReceiptFields | None = None,
) -> EvaluationCase:
    selected = data if data is not None else case_id.encode("ascii")
    image = ImagePayload(
        name=f"{case_id}.png",
        media_type="image/png",
        data=selected,
        sha256=hashlib.sha256(selected).hexdigest(),
        width=8,
        height=6,
    )
    return EvaluationCase(
        case_id=case_id,
        input=descriptor_for(image),
        truth=truth or _receipt(),
        candidate=candidate or _receipt(),
    )


def _suite(cases: Sequence[EvaluationCase] | None = None) -> EvaluationSuite:
    return build_evaluation_suite(
        name="balanced-field-outcomes-v1",
        cases=tuple(cases or (_case("case-one"), _case("case-two"))),
    )


def test_evaluator_contract_identity_is_pinned_and_explicit() -> None:
    identity = evaluator_contract_identity()

    assert identity.id == EVALUATOR_ID
    assert identity.schema_version == 1
    assert (
        identity.digest
        == evaluator_contract_digest()
        == ("sha256:0e7fd754958dce9f3e426b8988716296240e15e7cd98588c394d57675f5f55e2")
    )
    assert FIELD_ORDER == ("date", "amount", "vendor", "category")
    assert tuple(ReceiptFields.model_fields) == FIELD_ORDER
    assert OUTCOME_ORDER == ("exact", "omission", "spurious", "substitution")
    assert CATEGORY_LABELS == (
        "<null>",
        "Meals",
        "Transport",
        "Lodging",
        "Office Supplies",
        "Entertainment",
        "Other",
    )
    assert CATEGORY_LABELS[1:] == tuple(item.value for item in ExpenseCategory)
    assert receipt_contract_digest() == (
        "sha256:a41dc34788b12c26540266a99c03aa6aecbe70df7250b266676e5fce55f268b2"
    )

    for value in (True, 1.0, "1"):
        document = identity.model_dump(mode="json")
        document["schema_version"] = value
        with pytest.raises(ValidationError, match="integer 1"):
            type(identity).model_validate_json(
                json.dumps(document),
                strict=True,
            )


def test_suite_is_content_addressed_canonical_and_descriptor_bound() -> None:
    suite = _suite()
    text = evaluation_suite_json(suite)
    decoded = json.loads(text)

    assert suite.kind == "receipt-extractor-evaluation-suite"
    assert suite.schema_version == 1
    assert suite.suite_id == suite_id_for(suite.body)
    assert suite.body.input_batch_digest == batch_digest(
        [case.input for case in suite.body.cases]
    )
    assert suite.body.truth_origin == "repository-authored-synthetic"
    assert suite.body.evaluator == evaluator_contract_identity()
    assert suite.body.candidate == NegativeControlIdentity(
        id="authored-negative-control/v1",
        origin="repository-authored-deliberately-imperfect",
        live_model_output=False,
    )
    assert text == (
        json.dumps(decoded, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )
    assert suite.suite_id == (
        "sha256:bd0b273ea6f8d5e738cdcba925704181b4a6f573be364cca87a8c18a809f3b94"
    )


def test_suite_identity_changes_with_order_input_truth_or_candidate() -> None:
    first = _case("case-one")
    second = _case("case-two")
    baseline = _suite((first, second)).suite_id
    changed_input = _case("case-one", data=b"changed")
    changed_truth = first.model_copy(
        update={"truth": _receipt(vendor="Different Truth")}
    )
    changed_candidate = first.model_copy(update={"candidate": _receipt(amount=None)})

    assert _suite((second, first)).suite_id != baseline
    assert _suite((changed_input, second)).suite_id != baseline
    assert _suite((changed_truth, second)).suite_id != baseline
    assert _suite((changed_candidate, second)).suite_id != baseline


def test_public_builders_revalidate_model_copy_mutations() -> None:
    case = _case("case-one")
    mutated_case = case.model_copy(update={"input": object()})
    suite = _suite((case,))
    mutated_body = suite.body.model_copy(update={"name": "UPPERCASE"})
    changed_evaluator = suite.body.evaluator.model_copy(
        update={"digest": "sha256:" + "0" * 64}
    )
    changed_contract_body = suite.body.model_copy(
        update={"evaluator": changed_evaluator}
    )

    with pytest.raises(EvaluationError, match="could not build"):
        _suite((mutated_case,))
    with pytest.raises(EvaluationError, match=r"suite body.*schema v1"):
        suite_id_for(mutated_body)
    with pytest.raises(EvaluationError, match=r"suite body.*schema v1"):
        suite_id_for(cast(EvaluationSuiteBody, object()))
    with pytest.raises(EvaluationError, match=r"suite body.*schema v1"):
        suite_id_for(changed_contract_body)


def test_suite_body_rejects_duplicate_case_or_input_names_and_bad_digest() -> None:
    first = _case("case-one")
    duplicate_id = _case("case-one", data=b"second")
    duplicate_name = _case("case-two").model_copy(update={"input": first.input})
    identity = NegativeControlIdentity(
        id="authored-negative-control/v1",
        origin="repository-authored-deliberately-imperfect",
        live_model_output=False,
    )

    with pytest.raises(ValidationError, match="case IDs must be unique"):
        EvaluationSuiteBody(
            name="duplicate-case",
            truth_origin="repository-authored-synthetic",
            evaluator=evaluator_contract_identity(),
            candidate=identity,
            input_batch_digest=batch_digest([first.input, duplicate_id.input]),
            cases=(first, duplicate_id),
        )
    with pytest.raises(ValidationError, match="input names must be unique"):
        EvaluationSuiteBody(
            name="duplicate-input",
            truth_origin="repository-authored-synthetic",
            evaluator=evaluator_contract_identity(),
            candidate=identity,
            input_batch_digest=batch_digest([first.input, duplicate_name.input]),
            cases=(first, duplicate_name),
        )
    with pytest.raises(ValidationError, match="batch digest does not match"):
        EvaluationSuiteBody(
            name="bad-digest",
            truth_origin="repository-authored-synthetic",
            evaluator=evaluator_contract_identity(),
            candidate=identity,
            input_batch_digest="sha256:" + "0" * 64,
            cases=(first,),
        )


@pytest.mark.parametrize(
    "name",
    (
        ".",
        "..",
        "/home/omar/receipt.png",
        "../receipt.png",
        "nested/receipt.png",
        "C:receipt.png",
        "receipt.png:stream",
        r"C:\\Users\\Omar\\receipt.png",
    ),
)
def test_evaluation_case_rejects_path_bearing_input_names(name: str) -> None:
    input_descriptor = _case("case-one").input.model_copy(update={"name": name})

    with pytest.raises(ValidationError, match="direct-child"):
        EvaluationCase(
            case_id="case-one",
            input=input_descriptor,
            truth=_receipt(),
            candidate=_receipt(),
        )


@pytest.mark.parametrize("value", (0, 0.0, True, None))
def test_negative_control_requires_literal_false_boolean(value: object) -> None:
    document = {
        "id": "authored-negative-control/v1",
        "origin": "repository-authored-deliberately-imperfect",
        "live_model_output": value,
    }

    with pytest.raises(ValidationError, match="boolean false"):
        NegativeControlIdentity.model_validate_json(
            json.dumps(document),
            strict=True,
        )


def test_suite_rejects_tampered_id_and_strict_schema_changes() -> None:
    document = json.loads(evaluation_suite_json(_suite()))
    document["suite_id"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="suite ID does not match"):
        EvaluationSuite.model_validate_json(json.dumps(document), strict=True)

    document = json.loads(evaluation_suite_json(_suite()))
    document["unexpected"] = False
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationSuite.model_validate_json(json.dumps(document), strict=True)

    document = json.loads(evaluation_suite_json(_suite()))
    document["schema_version"] = True
    with pytest.raises(ValidationError):
        EvaluationSuite.model_validate_json(json.dumps(document), strict=True)


def test_builder_rejects_empty_oversized_wrong_type_and_broken_iterables() -> None:
    with pytest.raises(EvaluationError, match="at least one"):
        build_evaluation_suite(name="empty-suite", cases=())

    oversized = tuple(
        _case(f"case-{index}", data=index.to_bytes(2, "big"))
        for index in range(MAX_EVALUATION_CASES + 1)
    )
    with pytest.raises(EvaluationError, match="exceed the suite limit"):
        build_evaluation_suite(name="oversized-suite", cases=oversized)

    with pytest.raises(EvaluationError, match="could not build"):
        build_evaluation_suite(
            name="wrong-type",
            cases=[_case("case-one"), object()],  # type: ignore[list-item]
        )

    class BrokenCases(Sequence[EvaluationCase]):
        @overload
        def __getitem__(self, index: int) -> EvaluationCase: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[EvaluationCase]: ...

        def __getitem__(
            self, index: int | slice
        ) -> EvaluationCase | Sequence[EvaluationCase]:
            raise RuntimeError(str(index))

        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Any:
            raise RuntimeError("broken")

    with pytest.raises(EvaluationError, match="cannot be consumed safely"):
        build_evaluation_suite(name="broken-suite", cases=BrokenCases())


def test_serialization_revalidates_model_copy_mutations() -> None:
    suite = _suite()
    mutated = suite.model_copy(update={"suite_id": "sha256:" + "0" * 64})

    with pytest.raises(EvaluationError, match="could not serialize"):
        evaluation_suite_json(mutated)
