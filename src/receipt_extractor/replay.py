"""Strict, exact-batch offline replay for receipt extraction."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from receipt_extractor.artifact_io import (
    ArtifactIOError,
    ArtifactIOErrorCode,
    load_json_artifact,
)
from receipt_extractor.file_io import (
    MAX_DIRECTORY_ENTRIES,
    MAX_IMAGE_PIXELS,
    ImagePayload,
)
from receipt_extractor.schema import ReceiptFields

MAX_REPLAY_BYTES = 1024 * 1024
REPLAY_KIND = "receipt-extractor-replay"
_DIGEST_PREFIX = b"auditable-receipt-extractor/replay-batch/v1\0"
_Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_MediaType = Literal["image/jpeg", "image/png", "image/webp"]


class ReplayError(ValueError):
    """Raised when replay evidence is unsafe, malformed, or mismatched."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReplayInputDescriptor(_StrictModel):
    """Path-free identity of one already validated input image."""

    name: Annotated[str, Field(min_length=1, max_length=255)]
    media_type: _MediaType
    size_bytes: Annotated[int, Field(ge=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    width: Annotated[int, Field(ge=1, le=MAX_IMAGE_PIXELS)]
    height: Annotated[int, Field(ge=1, le=MAX_IMAGE_PIXELS)]

    @model_validator(mode="after")
    def validate_name_and_pixels(self) -> Self:
        try:
            encoded_name = os.fsencode(self.name)
        except UnicodeError as error:
            raise ValueError("replay input name is not safely encodable") from error
        if any(
            unicodedata.category(character).startswith("C") for character in self.name
        ):
            raise ValueError("replay input name contains a control character")
        if len(encoded_name) > 255:
            raise ValueError("replay input name exceeds the byte-length limit")
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ValueError("replay input exceeds the decoded pixel limit")
        return self


class ReplayItem(_StrictModel):
    """One exact input descriptor and its typed offline output."""

    input: ReplayInputDescriptor
    output: ReceiptFields


class ReplayBatch(_StrictModel):
    """Ordered replay batch protected against accidental dataset mismatch."""

    digest: _Digest
    items: Annotated[
        list[ReplayItem], Field(min_length=1, max_length=MAX_DIRECTORY_ENTRIES)
    ]

    @model_validator(mode="after")
    def reject_duplicate_inputs(self) -> Self:
        names = [item.input.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("replay inputs must have unique names")
        return self


class ReplayManifest(_StrictModel):
    """Versioned top-level replay document."""

    kind: Literal["receipt-extractor-replay"]
    schema_version: Literal[1]
    batch: ReplayBatch


_ARTIFACT_ERROR_MESSAGES = {
    ArtifactIOErrorCode.INVALID_SIZE_LIMIT: (
        "the replay manifest size limit is invalid"
    ),
    ArtifactIOErrorCode.PATH_NOT_ENCODABLE: "the replay path is not safely encodable",
    ArtifactIOErrorCode.PATH_CONTROL_CHARACTER: (
        "the replay path contains a control or format character"
    ),
    ArtifactIOErrorCode.JSON_PATH_REQUIRED: ("the replay path must name a .json file"),
    ArtifactIOErrorCode.PARENT_TRAVERSAL: (
        "parent traversal is not accepted in replay paths"
    ),
    ArtifactIOErrorCode.PARENT_ADVANCE_FAILED: (
        "could not safely advance through the replay parent"
    ),
    ArtifactIOErrorCode.PARENT_OPEN_FAILED: ("could not safely open the replay parent"),
    ArtifactIOErrorCode.BOUNDED_REGULAR_FILE_REQUIRED: (
        "the replay manifest must be a bounded single-link regular file"
    ),
    ArtifactIOErrorCode.FILE_CHANGED_BEFORE_OPEN: (
        "the replay manifest changed before it was opened"
    ),
    ArtifactIOErrorCode.FILE_CHANGED_DURING_READ: (
        "the replay manifest changed while it was read"
    ),
    ArtifactIOErrorCode.PATH_CHANGED_DURING_VALIDATION: (
        "the replay path changed during validation"
    ),
    ArtifactIOErrorCode.READ_FAILED: "could not safely read the replay manifest",
    ArtifactIOErrorCode.CLOSE_FAILED: "could not safely close the replay manifest",
    ArtifactIOErrorCode.INVALID_UTF8: ("the replay manifest must be strict UTF-8"),
    ArtifactIOErrorCode.UTF8_BOM: ("the replay manifest must not contain a UTF-8 BOM"),
    ArtifactIOErrorCode.DUPLICATE_JSON_KEY: (
        "the replay manifest contains a duplicate JSON key"
    ),
    ArtifactIOErrorCode.NONFINITE_JSON_VALUE: (
        "the replay manifest contains a non-finite JSON value"
    ),
    ArtifactIOErrorCode.INVALID_JSON: ("the replay manifest does not match schema v1"),
}


def descriptor_for(image: ImagePayload) -> ReplayInputDescriptor:
    """Build the canonical replay identity for one validated image."""
    try:
        return ReplayInputDescriptor(
            name=image.name,
            media_type=cast(_MediaType, image.media_type),
            size_bytes=image.size_bytes,
            sha256=image.sha256,
            width=image.width,
            height=image.height,
        )
    except ValidationError as error:
        raise ReplayError(
            "validated image metadata cannot be represented by replay schema v1"
        ) from error


def batch_digest(descriptors: Sequence[ReplayInputDescriptor]) -> str:
    """Hash canonical ordered descriptors as a mismatch guard, not a signature."""
    canonical = json.dumps(
        {"images": [descriptor.model_dump(mode="json") for descriptor in descriptors]},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(_DIGEST_PREFIX + canonical).hexdigest()}"


def _load_json(path: Path) -> tuple[Any, bytes]:
    try:
        artifact = load_json_artifact(path, max_bytes=MAX_REPLAY_BYTES)
    except ArtifactIOError as error:
        raise ReplayError(_ARTIFACT_ERROR_MESSAGES[error.code]) from error
    return artifact.value, artifact.raw_bytes


def load_manifest(path: Path) -> tuple[ReplayManifest, str]:
    """Safely load and validate a replay manifest and its raw-file digest."""
    decoded, data = _load_json(path)
    try:
        validated_json = json.dumps(
            decoded,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        manifest = ReplayManifest.model_validate_json(validated_json, strict=True)
    except (RecursionError, UnicodeError, ValueError, ValidationError) as error:
        raise ReplayError("the replay manifest does not match schema v1") from error

    descriptors = [item.input for item in manifest.batch.items]
    if not hmac.compare_digest(manifest.batch.digest, batch_digest(descriptors)):
        raise ReplayError("the replay batch digest does not match its inputs")
    raw_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return manifest, raw_digest


@dataclass(slots=True)
class ReplayProvider:
    """Bound exact-batch provider that never performs a network request."""

    items: tuple[ReplayItem, ...]
    manifest_sha256: str
    _cursor: int = 0

    @classmethod
    def bind(cls, path: Path, images: Sequence[ImagePayload]) -> ReplayProvider:
        manifest, manifest_sha256 = load_manifest(path)
        actual = [descriptor_for(image) for image in images]
        expected = [item.input for item in manifest.batch.items]
        if actual != expected or not hmac.compare_digest(
            batch_digest(actual),
            manifest.batch.digest,
        ):
            raise ReplayError("the replay manifest does not match the input batch")
        return cls(
            items=tuple(manifest.batch.items),
            manifest_sha256=manifest_sha256,
        )

    def __call__(self, image: ImagePayload) -> dict[str, Any]:
        if self._cursor >= len(self.items):
            raise ReplayError("the replay batch was consumed more than once")
        item = self.items[self._cursor]
        if descriptor_for(image) != item.input:
            raise ReplayError("the replay input order changed after binding")
        self._cursor += 1
        return item.output.model_dump(mode="json")

    def finalize(self) -> None:
        """Require every bound replay record to have been consumed exactly once."""
        if self._cursor != len(self.items):
            raise ReplayError("the replay batch was not consumed completely")
