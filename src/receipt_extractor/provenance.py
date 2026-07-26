"""Content-addressed provenance for exact-batch offline replay runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unicodedata
from collections.abc import Sequence
from itertools import islice
from pathlib import Path
from typing import Annotated, Any, Final, Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from receipt_extractor.artifact_io import (
    ArtifactIOError,
    ArtifactIOErrorCode,
    load_json_artifact,
)
from receipt_extractor.file_io import MAX_DIRECTORY_ENTRIES
from receipt_extractor.replay import (
    MAX_REPLAY_BYTES,
    ReplayInputDescriptor,
    ReplayManifest,
    batch_digest,
)
from receipt_extractor.schema import ReceiptFields

RUN_KIND: Final = "receipt-extractor-run"
MAX_REPLAY_RUN_BYTES = MAX_REPLAY_BYTES

_CONTRACT_DOMAIN_V1 = b"auditable-receipt-extractor/receipt-contract/v1\0"
_RUN_DOMAIN_V1 = b"auditable-receipt-extractor/replay-run/v1\0"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]


class ProvenanceError(ValueError):
    """Raised when a replay run is malformed or does not match its inputs."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used by provenance identities."""
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ProvenanceError("the provenance value is not canonical JSON") from error


_CONTRACT_DOCUMENT_V1: Final[dict[str, object]] = {
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
_CONTRACT_CANONICAL_V1 = canonical_json_bytes(_CONTRACT_DOCUMENT_V1)
_CONTRACT_DIGEST_V1 = (
    "sha256:a41dc34788b12c26540266a99c03aa6aecbe70df7250b266676e5fce55f268b2"
)

if len(_CONTRACT_CANONICAL_V1) != 780:  # pragma: no cover - import-time invariant
    raise RuntimeError("receipt contract v1 canonical length changed")
if (
    f"sha256:{hashlib.sha256(_CONTRACT_DOMAIN_V1 + _CONTRACT_CANONICAL_V1).hexdigest()}"
    != _CONTRACT_DIGEST_V1
):  # pragma: no cover - import-time invariant
    raise RuntimeError("receipt contract v1 identity changed")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReceiptContractIdentity(_StrictModel):
    """Versioned identity of the exact receipt-field validation contract."""

    id: Literal["receipt-extractor/receipt-fields"]
    schema_version: Literal[1]
    digest: _Digest


class ReplayRunItem(_StrictModel):
    """One output bound to one safe, direct-child replay input name."""

    input_name: Annotated[str, Field(min_length=1, max_length=255)]
    output: ReceiptFields

    @model_validator(mode="after")
    def validate_input_name(self) -> Self:
        if self.input_name in {".", ".."} or "/" in self.input_name:
            raise ValueError("run input name must be a direct-child POSIX name")
        try:
            encoded_name = os.fsencode(self.input_name)
        except UnicodeError as error:
            raise ValueError("run input name is not safely encodable") from error
        if len(encoded_name) > 255:
            raise ValueError("run input name exceeds the byte-length limit")
        if any(
            unicodedata.category(character).startswith("C")
            for character in self.input_name
        ):
            raise ValueError("run input name contains a control character")
        return self


class ReplayRunBody(_StrictModel):
    """Every replay-run binding covered by the whole-body run identity."""

    mode: Literal["replay"]
    contract: ReceiptContractIdentity
    input_batch_digest: _Digest
    replay_manifest_file_sha256: _Digest
    items: Annotated[
        tuple[ReplayRunItem, ...],
        Field(min_length=1, max_length=MAX_DIRECTORY_ENTRIES),
    ]

    @model_validator(mode="after")
    def reject_duplicate_inputs(self) -> Self:
        names = [item.input_name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("run inputs must have unique names")
        return self


class ReplayRun(_StrictModel):
    """Versioned, content-addressed replay-run document."""

    kind: Literal["receipt-extractor-run"]
    schema_version: Literal[1]
    run_id: _Digest
    body: ReplayRunBody


def receipt_contract_digest() -> str:
    """Return the pinned identity of the explicit receipt contract v1."""
    return (
        "sha256:"
        + hashlib.sha256(_CONTRACT_DOMAIN_V1 + _CONTRACT_CANONICAL_V1).hexdigest()
    )


def run_id_for(body: ReplayRunBody) -> str:
    """Hash the complete canonical run body, excluding only ``run_id``."""
    validated_body = _validated_model(
        body,
        ReplayRunBody,
        label="the replay run body",
    )
    payload = {
        "kind": RUN_KIND,
        "schema_version": 1,
        "body": validated_body.model_dump(mode="json"),
    }
    digest = hashlib.sha256(_RUN_DOMAIN_V1 + canonical_json_bytes(payload)).hexdigest()
    return f"sha256:{digest}"


_ARTIFACT_ERROR_MESSAGES = {
    ArtifactIOErrorCode.INVALID_SIZE_LIMIT: "the replay run size limit is invalid",
    ArtifactIOErrorCode.PATH_NOT_ENCODABLE: (
        "the replay run path is not safely encodable"
    ),
    ArtifactIOErrorCode.PATH_CONTROL_CHARACTER: (
        "the replay run path contains a control or format character"
    ),
    ArtifactIOErrorCode.JSON_PATH_REQUIRED: (
        "the replay run path must name a .json file"
    ),
    ArtifactIOErrorCode.PARENT_TRAVERSAL: (
        "parent traversal is not accepted in replay run paths"
    ),
    ArtifactIOErrorCode.PARENT_ADVANCE_FAILED: (
        "could not safely advance through the replay run parent"
    ),
    ArtifactIOErrorCode.PARENT_OPEN_FAILED: (
        "could not safely open the replay run parent"
    ),
    ArtifactIOErrorCode.BOUNDED_REGULAR_FILE_REQUIRED: (
        "the replay run must be a bounded single-link regular file"
    ),
    ArtifactIOErrorCode.FILE_CHANGED_BEFORE_OPEN: (
        "the replay run changed before it was opened"
    ),
    ArtifactIOErrorCode.FILE_CHANGED_DURING_READ: (
        "the replay run changed while it was read"
    ),
    ArtifactIOErrorCode.PATH_CHANGED_DURING_VALIDATION: (
        "the replay run path changed during validation"
    ),
    ArtifactIOErrorCode.READ_FAILED: "could not safely read the replay run",
    ArtifactIOErrorCode.CLOSE_FAILED: "could not safely close the replay run",
    ArtifactIOErrorCode.INVALID_UTF8: "the replay run must be strict UTF-8",
    ArtifactIOErrorCode.UTF8_BOM: "the replay run must not contain a UTF-8 BOM",
    ArtifactIOErrorCode.DUPLICATE_JSON_KEY: (
        "the replay run contains a duplicate JSON key"
    ),
    ArtifactIOErrorCode.NONFINITE_JSON_VALUE: (
        "the replay run contains a non-finite JSON value"
    ),
    ArtifactIOErrorCode.INVALID_JSON: "the replay run does not match schema v1",
}


def _fail(message: str) -> NoReturn:
    raise ProvenanceError(message)


def _validated_model[ModelT: BaseModel](
    value: object,
    model_type: type[ModelT],
    *,
    label: str,
) -> ModelT:
    if not isinstance(value, model_type):
        _fail(f"{label} does not match schema v1")
    try:
        encoded = canonical_json_bytes(value.model_dump(mode="json"))
        return model_type.model_validate_json(encoded, strict=True)
    except (ProvenanceError, RecursionError, UnicodeError, ValueError) as error:
        raise ProvenanceError(f"{label} does not match schema v1") from error


def _validated_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(_DIGEST_PATTERN, value) is None:
        _fail(f"{label} is not a canonical SHA-256 digest")
    return value


def _bounded_tuple[ValueT](
    values: Sequence[ValueT],
    *,
    label: str,
) -> tuple[ValueT, ...]:
    try:
        selected = tuple(islice(iter(values), MAX_DIRECTORY_ENTRIES + 1))
    except Exception as error:
        raise ProvenanceError(f"{label} cannot be consumed safely") from error
    if len(selected) > MAX_DIRECTORY_ENTRIES:
        _fail(f"{label} exceeds the batch limit")
    return selected


def load_replay_run(path: Path) -> ReplayRun:
    """Load a strict run document through the shared pinned artifact boundary."""
    if not isinstance(path, Path):
        _fail("the replay run path is invalid")
    try:
        artifact = load_json_artifact(path, max_bytes=MAX_REPLAY_RUN_BYTES)
    except ArtifactIOError as error:
        raise ProvenanceError(_ARTIFACT_ERROR_MESSAGES[error.code]) from error
    try:
        return ReplayRun.model_validate_json(artifact.raw_bytes, strict=True)
    except (RecursionError, UnicodeError, ValueError, ValidationError) as error:
        raise ProvenanceError("the replay run does not match schema v1") from error


def _manifest_expectations(
    manifest: ReplayManifest,
) -> tuple[
    ReplayManifest,
    tuple[ReplayInputDescriptor, ...],
    tuple[ReplayRunItem, ...],
]:
    validated_manifest = _validated_model(
        manifest,
        ReplayManifest,
        label="the replay manifest",
    )
    descriptors = tuple(item.input for item in validated_manifest.batch.items)
    if not hmac.compare_digest(
        validated_manifest.batch.digest,
        batch_digest(descriptors),
    ):
        _fail("the replay manifest batch digest does not match its inputs")
    try:
        items = tuple(
            ReplayRunItem(input_name=item.input.name, output=item.output)
            for item in validated_manifest.batch.items
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ProvenanceError("the replay manifest does not match schema v1") from error
    return validated_manifest, descriptors, items


def build_replay_run(
    *,
    manifest: ReplayManifest,
    manifest_file_sha256: str,
    materialized_items: Sequence[tuple[str, ReceiptFields]],
) -> ReplayRun:
    """Build one run only from exact ordered, typed manifest outputs."""
    validated_manifest, _, expected_items = _manifest_expectations(manifest)
    validated_manifest_sha256 = _validated_digest(
        manifest_file_sha256,
        label="the replay manifest file digest",
    )
    supplied_items = _bounded_tuple(
        materialized_items,
        label="the materialized replay items",
    )
    try:
        actual_items_list: list[ReplayRunItem] = []
        for supplied in supplied_items:
            if not isinstance(supplied, tuple) or len(supplied) != 2:
                _fail("the materialized replay items do not match schema v1")
            name, output = supplied
            if not isinstance(output, ReceiptFields):
                _fail("the materialized replay items do not match schema v1")
            validated_output = _validated_model(
                output,
                ReceiptFields,
                label="the materialized replay output",
            )
            actual_items_list.append(
                ReplayRunItem(input_name=name, output=validated_output)
            )
        actual_items = tuple(actual_items_list)
    except (ProvenanceError, TypeError, ValueError, ValidationError) as error:
        raise ProvenanceError(
            "the materialized replay items do not match schema v1"
        ) from error
    if actual_items != expected_items:
        _fail("the materialized replay items do not match the replay manifest")

    try:
        body = ReplayRunBody(
            mode="replay",
            contract=ReceiptContractIdentity(
                id="receipt-extractor/receipt-fields",
                schema_version=1,
                digest=receipt_contract_digest(),
            ),
            input_batch_digest=validated_manifest.batch.digest,
            replay_manifest_file_sha256=validated_manifest_sha256,
            items=actual_items,
        )
        run = ReplayRun(
            kind=RUN_KIND,
            schema_version=1,
            run_id=run_id_for(body),
            body=body,
        )
        return ReplayRun.model_validate_json(
            canonical_json_bytes(run.model_dump(mode="json")),
            strict=True,
        )
    except (RecursionError, UnicodeError, ValueError, ValidationError) as error:
        raise ProvenanceError("could not build replay run schema v1") from error


def verify_replay_run(
    *,
    run: ReplayRun,
    manifest: ReplayManifest,
    manifest_file_sha256: str,
    descriptors: Sequence[ReplayInputDescriptor],
) -> None:
    """Verify every run binding against one manifest and current input batch."""
    validated_run = _validated_model(
        run,
        ReplayRun,
        label="the replay run",
    )
    validated_manifest, expected_descriptors, expected_items = _manifest_expectations(
        manifest
    )
    validated_manifest_sha256 = _validated_digest(
        manifest_file_sha256,
        label="the replay manifest file digest",
    )
    supplied_descriptors = _bounded_tuple(
        descriptors,
        label="the current input descriptors",
    )
    current_descriptors = tuple(
        _validated_model(
            descriptor,
            ReplayInputDescriptor,
            label="the current input descriptor",
        )
        for descriptor in supplied_descriptors
    )
    current_digest = batch_digest(current_descriptors)

    if current_descriptors != expected_descriptors:
        _fail("the current input descriptors do not match the replay manifest")
    if not (
        hmac.compare_digest(current_digest, validated_manifest.batch.digest)
        and hmac.compare_digest(
            current_digest,
            validated_run.body.input_batch_digest,
        )
    ):
        _fail("the replay run input batch digest does not match")
    if validated_run.body.items != expected_items:
        _fail("the replay run items do not match the replay manifest")

    expected_contract = ReceiptContractIdentity(
        id="receipt-extractor/receipt-fields",
        schema_version=1,
        digest=receipt_contract_digest(),
    )
    if validated_run.body.contract != expected_contract:
        _fail("the replay run receipt contract does not match")
    if not hmac.compare_digest(
        validated_run.body.replay_manifest_file_sha256,
        validated_manifest_sha256,
    ):
        _fail("the replay run does not bind the exact replay manifest file")
    if not hmac.compare_digest(
        validated_run.run_id,
        run_id_for(validated_run.body),
    ):
        _fail("the replay run identity does not match its body")
