from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, overload

import pytest
from pydantic import ValidationError

import receipt_extractor.provenance as provenance
from receipt_extractor.artifact_io import ArtifactIOErrorCode
from receipt_extractor.provenance import (
    MAX_REPLAY_RUN_BYTES,
    ProvenanceError,
    ReceiptContractIdentity,
    ReplayRun,
    ReplayRunBody,
    ReplayRunItem,
    build_replay_run,
    canonical_json_bytes,
    load_replay_run,
    receipt_contract_digest,
    run_id_for,
    verify_replay_run,
)
from receipt_extractor.replay import (
    ReplayBatch,
    ReplayInputDescriptor,
    ReplayItem,
    ReplayManifest,
    batch_digest,
)
from receipt_extractor.schema import ExpenseCategory, ReceiptFields

_ONE_DIGEST = f"sha256:{'1' * 64}"
_TWO_DIGEST = f"sha256:{'2' * 64}"


def _output(
    vendor: str = "Synthetic Market",
    *,
    date: str | None = "2026-07-24",
    amount: str | None = "$12.50",
    category: ExpenseCategory | None = ExpenseCategory.OTHER,
) -> ReceiptFields:
    return ReceiptFields(
        date=date,
        amount=amount,
        vendor=vendor,
        category=category,
    )


def _descriptor(
    name: str,
    content: bytes,
    *,
    width: int = 3,
    height: int = 2,
) -> ReplayInputDescriptor:
    return ReplayInputDescriptor(
        name=name,
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        width=width,
        height=height,
    )


def _manifest(
    descriptors: Sequence[ReplayInputDescriptor] | None = None,
    outputs: Sequence[ReceiptFields] | None = None,
) -> ReplayManifest:
    selected_descriptors = list(
        descriptors
        if descriptors is not None
        else [
            _descriptor("a.png", b"synthetic-a"),
            _descriptor("b.png", b"synthetic-b", width=4, height=5),
        ]
    )
    selected_outputs = list(
        outputs if outputs is not None else [_output("Vendor A"), _output("Vendor B")]
    )
    assert len(selected_descriptors) == len(selected_outputs)
    return ReplayManifest(
        kind="receipt-extractor-replay",
        schema_version=1,
        batch=ReplayBatch(
            digest=batch_digest(selected_descriptors),
            items=[
                ReplayItem(input=descriptor, output=output)
                for descriptor, output in zip(
                    selected_descriptors,
                    selected_outputs,
                    strict=True,
                )
            ],
        ),
    )


def _build(
    manifest: ReplayManifest | None = None,
    *,
    manifest_digest: str = _TWO_DIGEST,
) -> tuple[ReplayRun, ReplayManifest]:
    selected_manifest = manifest or _manifest()
    run = build_replay_run(
        manifest=selected_manifest,
        manifest_file_sha256=manifest_digest,
        materialized_items=[
            (item.input.name, item.output) for item in selected_manifest.batch.items
        ],
    )
    return run, selected_manifest


def _body_with(
    run: ReplayRun,
    **updates: object,
) -> ReplayRun:
    body = run.body.model_copy(update=updates)
    return run.model_copy(
        update={
            "body": body,
            "run_id": run_id_for(body),
        }
    )


class EndlessSequence[ValueT](Sequence[ValueT]):
    """A Sequence whose length lies and whose index stream never terminates."""

    def __init__(self, value: ValueT) -> None:
        self.value = value

    @overload
    def __getitem__(self, index: int) -> ValueT: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[ValueT]: ...

    def __getitem__(self, index: int | slice) -> ValueT | Sequence[ValueT]:
        if isinstance(index, slice):
            return self
        return self.value

    def __len__(self) -> int:
        return 0


def test_contract_literal_and_domain_digest_are_exact_golden_vectors() -> None:
    expected = {
        "id": "receipt-extractor/receipt-fields",
        "schema_version": 1,
        "object": {
            "required": ["date", "amount", "vendor", "category"],
            "strict_scalar_types": True,
            "unknown_fields": "reject",
        },
        "fields": {
            "date": {
                "types": ["string", "null"],
                "max_length": 64,
                "strip_surrounding_whitespace": True,
                "blank_to_null": True,
                "reject_unicode_general_category_prefixes": ["C"],
            },
            "amount": {
                "types": ["string", "null"],
                "max_length": 64,
                "strip_surrounding_whitespace": True,
                "blank_to_null": True,
                "reject_unicode_general_category_prefixes": ["C"],
            },
            "vendor": {
                "types": ["string", "null"],
                "max_length": 200,
                "strip_surrounding_whitespace": True,
                "blank_to_null": True,
                "reject_unicode_general_category_prefixes": ["C"],
            },
            "category": {
                "types": ["enum", "null"],
                "values": [
                    "Meals",
                    "Transport",
                    "Lodging",
                    "Office Supplies",
                    "Entertainment",
                    "Other",
                ],
            },
        },
    }
    canonical = canonical_json_bytes(expected)

    assert expected == provenance._CONTRACT_DOCUMENT_V1
    assert canonical == provenance._CONTRACT_CANONICAL_V1
    assert len(canonical) == 780
    assert (
        receipt_contract_digest()
        == "sha256:a41dc34788b12c26540266a99c03aa6aecbe70df7250b266676e5fce55f268b2"
    )


def test_contract_literal_matches_receipt_fields_runtime_semantics() -> None:
    normalized = ReceiptFields.model_validate(
        {
            "date": " 2026-07-24 ",
            "amount": " ",
            "vendor": "\tSynthetic Market\n",
            "category": ExpenseCategory.OTHER,
        },
        strict=True,
    )

    assert normalized.date == "2026-07-24"
    assert normalized.amount is None
    assert normalized.vendor == "Synthetic Market"
    assert tuple(category.value for category in ExpenseCategory) == (
        "Meals",
        "Transport",
        "Lodging",
        "Office Supplies",
        "Entertainment",
        "Other",
    )

    for field, too_long in (
        ("date", "d" * 65),
        ("amount", "a" * 65),
        ("vendor", "v" * 201),
    ):
        document: dict[str, object] = {
            "date": None,
            "amount": None,
            "vendor": None,
            "category": None,
        }
        document[field] = too_long
        with pytest.raises(ValidationError):
            ReceiptFields.model_validate(document, strict=True)

    for mutation in (
        {"date": 20260724},
        {"amount": 12.50},
        {"vendor": True},
        {"date": "safe\u0000unsafe"},
        {"unexpected": None},
    ):
        document = {
            "date": None,
            "amount": None,
            "vendor": None,
            "category": None,
            **mutation,
        }
        with pytest.raises(ValidationError):
            ReceiptFields.model_validate(document, strict=True)

    valid_json_category = ReceiptFields.model_validate_json(
        canonical_json_bytes(
            {
                "date": None,
                "amount": None,
                "vendor": None,
                "category": "Other",
            }
        ),
        strict=True,
    )
    assert valid_json_category.category is ExpenseCategory.OTHER

    for category in (" Other ", "", "Travel", "Other\u0000"):
        with pytest.raises(ValidationError):
            ReceiptFields.model_validate_json(
                canonical_json_bytes(
                    {
                        "date": None,
                        "amount": None,
                        "vendor": None,
                        "category": category,
                    }
                ),
                strict=True,
            )

    with pytest.raises(ValidationError):
        ReceiptFields.model_validate(
            {
                "date": None,
                "amount": None,
                "vendor": None,
            },
            strict=True,
        )


def test_whole_body_run_id_is_the_exact_golden_vector() -> None:
    body = ReplayRunBody(
        mode="replay",
        contract=ReceiptContractIdentity(
            id="receipt-extractor/receipt-fields",
            schema_version=1,
            digest=receipt_contract_digest(),
        ),
        input_batch_digest=_ONE_DIGEST,
        replay_manifest_file_sha256=_TWO_DIGEST,
        items=(
            ReplayRunItem(
                input_name="cafe-lumen.png",
                output=_output(),
            ),
        ),
    )
    payload = {
        "kind": "receipt-extractor-run",
        "schema_version": 1,
        "body": body.model_dump(mode="json"),
    }

    assert len(canonical_json_bytes(payload)) == 568
    assert (
        run_id_for(body)
        == "sha256:bd504d9a148657ce07c1337a49e987a7b35094199db5c56937ea80f383625308"
    )


def test_run_id_revalidates_its_public_body_argument() -> None:
    run, _ = _build()
    invalid_body = run.body.model_copy(update={"mode": "live"})

    with pytest.raises(ProvenanceError, match=r"body.*schema v1"):
        run_id_for(invalid_body)
    with pytest.raises(ProvenanceError, match=r"body.*schema v1"):
        run_id_for(cast(ReplayRunBody, object()))


def test_canonical_json_is_sorted_ascii_compact_and_rejects_unsafe_values() -> None:
    assert canonical_json_bytes({"é": [2, 1], "a": True}) == (
        b'{"a":true,"\\u00e9":[2,1]}'
    )
    with pytest.raises(ProvenanceError, match="not canonical JSON"):
        canonical_json_bytes({"value": float("nan")})
    with pytest.raises(ProvenanceError, match="not canonical JSON"):
        canonical_json_bytes({"value": b"bytes"})
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ProvenanceError, match="not canonical JSON"):
        canonical_json_bytes(recursive)


@pytest.mark.parametrize(
    "input_name",
    [
        ".",
        "..",
        "nested/receipt.png",
        "/receipt.png",
        "receipt\u0000.png",
        "receipt\u202e.png",
        "\ud800.png",
        f"{'é' * 126}.png",
    ],
)
def test_run_item_rejects_unsafe_or_non_direct_child_names(
    input_name: str,
) -> None:
    with pytest.raises(ValidationError):
        ReplayRunItem(input_name=input_name, output=_output())


def test_run_models_are_strict_frozen_and_use_bounded_tuples() -> None:
    run, _ = _build()

    assert isinstance(run.body.items, tuple)
    with pytest.raises(ValidationError):
        run.body.__setattr__("mode", "live")
    with pytest.raises(ValidationError):
        ReplayRun.model_validate(
            {
                **run.model_dump(mode="python"),
                "schema_version": "1",
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        ReplayRunBody(
            **{
                **run.body.model_dump(mode="python"),
                "items": (run.body.items[0], run.body.items[0]),
            }
        )


def test_build_binds_exact_ordered_typed_manifest_outputs() -> None:
    run, manifest = _build()

    assert run.kind == "receipt-extractor-run"
    assert run.schema_version == 1
    assert run.body.mode == "replay"
    assert run.body.input_batch_digest == manifest.batch.digest
    assert run.body.replay_manifest_file_sha256 == _TWO_DIGEST
    assert run.body.contract.digest == receipt_contract_digest()
    assert run.body.items == tuple(
        ReplayRunItem(input_name=item.input.name, output=item.output)
        for item in manifest.batch.items
    )
    assert run.run_id == run_id_for(run.body)


@pytest.mark.parametrize(
    "mutation",
    ["order", "name", "output", "missing", "untyped-output", "list-pair"],
)
def test_build_rejects_every_materialized_association_mismatch(
    mutation: str,
) -> None:
    manifest = _manifest()
    materialized: list[Any] = [
        (item.input.name, item.output) for item in manifest.batch.items
    ]
    if mutation == "order":
        materialized.reverse()
    elif mutation == "name":
        materialized[0] = ("renamed.png", materialized[0][1])
    elif mutation == "output":
        materialized[0] = (materialized[0][0], _output("Wrong vendor"))
    elif mutation == "missing":
        materialized.pop()
    elif mutation == "untyped-output":
        materialized[0] = (
            materialized[0][0],
            materialized[0][1].model_dump(mode="json"),
        )
    else:
        materialized[0] = list(materialized[0])

    with pytest.raises(ProvenanceError, match="materialized replay items"):
        build_replay_run(
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            materialized_items=materialized,
        )


def test_build_rejects_invalid_manifest_digest_and_public_argument_types() -> None:
    manifest = _manifest()
    invalid_manifest = manifest.model_copy(
        update={"batch": manifest.batch.model_copy(update={"digest": _ONE_DIGEST})}
    )
    items = [(item.input.name, item.output) for item in manifest.batch.items]

    with pytest.raises(ProvenanceError, match="batch digest"):
        build_replay_run(
            manifest=invalid_manifest,
            manifest_file_sha256=_TWO_DIGEST,
            materialized_items=items,
        )
    with pytest.raises(ProvenanceError, match=r"manifest.*schema"):
        build_replay_run(
            manifest=cast(ReplayManifest, object()),
            manifest_file_sha256=_TWO_DIGEST,
            materialized_items=items,
        )
    with pytest.raises(ProvenanceError, match="canonical SHA-256"):
        build_replay_run(
            manifest=manifest,
            manifest_file_sha256=cast(str, object()),
            materialized_items=items,
        )


@pytest.mark.parametrize("operation", ["build", "verify"])
def test_public_operations_redact_manifest_names_outside_run_schema(
    operation: str,
) -> None:
    unsafe_descriptor = _descriptor("nested/receipt.png", b"synthetic")
    unsafe_manifest = _manifest(
        [unsafe_descriptor],
        [_output()],
    )

    with pytest.raises(ProvenanceError, match=r"manifest.*schema v1"):
        if operation == "build":
            build_replay_run(
                manifest=unsafe_manifest,
                manifest_file_sha256=_TWO_DIGEST,
                materialized_items=[
                    (
                        unsafe_manifest.batch.items[0].input.name,
                        unsafe_manifest.batch.items[0].output,
                    )
                ],
            )
        else:
            run, _ = _build()
            verify_replay_run(
                run=run,
                manifest=unsafe_manifest,
                manifest_file_sha256=_TWO_DIGEST,
                descriptors=[unsafe_descriptor],
            )


def test_build_bounds_a_sequence_even_when_len_lies() -> None:
    manifest = _manifest()
    item = manifest.batch.items[0]
    endless = EndlessSequence((item.input.name, item.output))

    with pytest.raises(ProvenanceError, match="exceeds the batch limit"):
        build_replay_run(
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            materialized_items=endless,
        )


def test_verify_accepts_only_the_exact_manifest_current_batch_and_run() -> None:
    run, manifest = _build()

    verify_replay_run(
        run=run,
        manifest=manifest,
        manifest_file_sha256=_TWO_DIGEST,
        descriptors=[item.input for item in manifest.batch.items],
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("descriptor-order", "current input descriptors"),
        ("descriptor-value", "current input descriptors"),
        ("input-digest", "input batch digest"),
        ("item-name", "items do not match"),
        ("item-output", "items do not match"),
        ("contract", "receipt contract"),
        ("manifest-file", "exact replay manifest file"),
        ("run-id", "identity does not match"),
        ("manifest-output", "items do not match"),
    ],
)
def test_verify_rejects_each_independent_binding_mismatch(
    mutation: str,
    message: str,
) -> None:
    run, manifest = _build()
    descriptors = [item.input for item in manifest.batch.items]
    manifest_digest = _TWO_DIGEST

    if mutation == "descriptor-order":
        descriptors.reverse()
    elif mutation == "descriptor-value":
        descriptors[0] = descriptors[0].model_copy(
            update={"width": descriptors[0].width + 1}
        )
    elif mutation == "input-digest":
        run = _body_with(run, input_batch_digest=_ONE_DIGEST)
    elif mutation == "item-name":
        changed = run.body.items[0].model_copy(update={"input_name": "renamed.png"})
        run = _body_with(run, items=(changed, *run.body.items[1:]))
    elif mutation == "item-output":
        changed = run.body.items[0].model_copy(
            update={"output": _output("Wrong vendor")}
        )
        run = _body_with(run, items=(changed, *run.body.items[1:]))
    elif mutation == "contract":
        contract = run.body.contract.model_copy(update={"digest": _ONE_DIGEST})
        run = _body_with(run, contract=contract)
    elif mutation == "manifest-file":
        manifest_digest = _ONE_DIGEST
    elif mutation == "run-id":
        run = run.model_copy(update={"run_id": _ONE_DIGEST})
    else:
        outputs = [
            _output("Changed manifest"),
            manifest.batch.items[1].output,
        ]
        manifest = _manifest(descriptors, outputs)

    with pytest.raises(ProvenanceError, match=message):
        verify_replay_run(
            run=run,
            manifest=manifest,
            manifest_file_sha256=manifest_digest,
            descriptors=descriptors,
        )


def test_verify_rejects_wrong_objects_invalid_digest_and_unbounded_sequence() -> None:
    run, manifest = _build()
    descriptors = [item.input for item in manifest.batch.items]

    with pytest.raises(ProvenanceError, match=r"run.*schema"):
        verify_replay_run(
            run=cast(ReplayRun, object()),
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            descriptors=descriptors,
        )
    with pytest.raises(ProvenanceError, match="canonical SHA-256"):
        verify_replay_run(
            run=run,
            manifest=manifest,
            manifest_file_sha256="not-a-digest",
            descriptors=descriptors,
        )
    with pytest.raises(ProvenanceError, match=r"descriptor.*schema"):
        verify_replay_run(
            run=run,
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            descriptors=[cast(ReplayInputDescriptor, object())],
        )
    with pytest.raises(ProvenanceError, match="exceeds the batch limit"):
        verify_replay_run(
            run=run,
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            descriptors=EndlessSequence(descriptors[0]),
        )


def test_run_id_changes_for_every_mutable_body_binding() -> None:
    run, _ = _build()
    baseline = run.run_id
    first, second = run.body.items
    bodies = [
        run.body.model_copy(update={"input_batch_digest": _ONE_DIGEST}),
        run.body.model_copy(update={"replay_manifest_file_sha256": _ONE_DIGEST}),
        run.body.model_copy(
            update={
                "contract": run.body.contract.model_copy(update={"digest": _ONE_DIGEST})
            }
        ),
        run.body.model_copy(update={"items": (second, first)}),
        run.body.model_copy(
            update={
                "items": (
                    first.model_copy(update={"input_name": "changed.png"}),
                    second,
                )
            }
        ),
    ]
    for field, value in (
        ("date", "2026-07-25"),
        ("amount", "$99.00"),
        ("vendor", "Changed vendor"),
        ("category", ExpenseCategory.MEALS),
    ):
        changed_output = first.output.model_copy(update={field: value})
        bodies.append(
            run.body.model_copy(
                update={
                    "items": (
                        first.model_copy(update={"output": changed_output}),
                        second,
                    )
                }
            )
        )

    assert all(run_id_for(body) != baseline for body in bodies)
    assert len({run_id_for(body) for body in bodies}) == len(bodies)


def test_load_uses_raw_json_bytes_and_is_stable_across_presentation(
    tmp_path: Path,
) -> None:
    run, _ = _build()
    document = run.model_dump(mode="json")
    compact_path = tmp_path / "compact.json"
    pretty_path = tmp_path / "pretty.json"
    compact_path.write_bytes(canonical_json_bytes(document))
    pretty_path.write_text(
        json.dumps(
            {
                "schema_version": document["schema_version"],
                "body": document["body"],
                "run_id": document["run_id"],
                "kind": document["kind"],
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    compact = load_replay_run(compact_path)
    pretty = load_replay_run(pretty_path)

    assert compact == run
    assert pretty == run
    assert isinstance(pretty.body.items, tuple)
    assert run_id_for(compact.body) == run_id_for(pretty.body)


def test_run_artifact_errors_have_an_exhaustive_stable_mapping() -> None:
    assert set(provenance._ARTIFACT_ERROR_MESSAGES) == set(ArtifactIOErrorCode)


def test_load_rejects_a_wrong_public_path_type() -> None:
    with pytest.raises(ProvenanceError, match="path is invalid"):
        load_replay_run(cast(Path, object()))


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("extra-top", True),
        ("missing-kind", None),
        ("coerced-version", "1"),
        ("bad-run-digest", f"sha256:{'A' * 64}"),
        ("extra-body", True),
        ("coerced-contract-version", "1"),
        ("bad-input-name", "../receipt.png"),
        ("numeric-vendor", 7),
        ("unknown-category", "Travel"),
    ],
)
def test_load_rejects_unknown_missing_coerced_and_malformed_fields(
    tmp_path: Path,
    mutation: str,
    value: object,
) -> None:
    run, _ = _build()
    document = copy.deepcopy(run.model_dump(mode="json"))
    if mutation == "extra-top":
        document["unexpected"] = value
    elif mutation == "missing-kind":
        del document["kind"]
    elif mutation == "coerced-version":
        document["schema_version"] = value
    elif mutation == "bad-run-digest":
        document["run_id"] = value
    elif mutation == "extra-body":
        document["body"]["unexpected"] = value
    elif mutation == "coerced-contract-version":
        document["body"]["contract"]["schema_version"] = value
    elif mutation == "bad-input-name":
        document["body"]["items"][0]["input_name"] = value
    elif mutation == "numeric-vendor":
        document["body"]["items"][0]["output"]["vendor"] = value
    else:
        document["body"]["items"][0]["output"]["category"] = value
    path = tmp_path / f"{mutation}.json"
    path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(ProvenanceError, match="schema v1"):
        load_replay_run(path)


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("duplicate", b'{"kind":"a","kind":"b"}', "duplicate JSON key"),
        ("bom", b"\xef\xbb\xbf{}", "UTF-8 BOM"),
        ("utf8", b'{"kind":"\xff"}', "strict UTF-8"),
        ("nan", b'{"kind":NaN}', "non-finite JSON"),
        ("infinity", b'{"kind":Infinity}', "non-finite JSON"),
    ],
)
def test_load_rejects_noncanonical_json_encodings(
    tmp_path: Path,
    name: str,
    raw: bytes,
    message: str,
) -> None:
    path = tmp_path / f"{name}.json"
    path.write_bytes(raw)

    with pytest.raises(ProvenanceError, match=message):
        load_replay_run(path)


def test_load_rejects_symlink_hardlink_fifo_and_oversized_artifacts(
    tmp_path: Path,
) -> None:
    run, _ = _build()
    target = tmp_path / "target.json"
    target.write_bytes(canonical_json_bytes(run.model_dump(mode="json")))

    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(ProvenanceError, match="single-link regular file"):
        load_replay_run(symlink)

    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(ProvenanceError, match="single-link regular file"):
        load_replay_run(hardlink)

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ProvenanceError, match="single-link regular file"):
        load_replay_run(fifo)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * MAX_REPLAY_RUN_BYTES)
    with pytest.raises(ProvenanceError, match="single-link regular file"):
        load_replay_run(oversized)


def test_load_is_schema_only_and_verify_independently_rechecks_run_id(
    tmp_path: Path,
) -> None:
    run, manifest = _build()
    document = run.model_dump(mode="json")
    document["run_id"] = _ONE_DIGEST
    path = tmp_path / "tampered.json"
    path.write_bytes(canonical_json_bytes(document))

    loaded = load_replay_run(path)

    assert loaded.run_id == _ONE_DIGEST
    with pytest.raises(ProvenanceError, match="identity does not match"):
        verify_replay_run(
            run=loaded,
            manifest=manifest,
            manifest_file_sha256=_TWO_DIGEST,
            descriptors=[item.input for item in manifest.batch.items],
        )
