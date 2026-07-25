"""Strict, exact-batch offline replay for receipt extraction."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, details: os.stat_result) -> _Identity:
        return cls(
            device=details.st_dev,
            inode=details.st_ino,
            mode=details.st_mode,
            links=details.st_nlink,
            size=details.st_size,
            modified_ns=details.st_mtime_ns,
            changed_ns=details.st_ctime_ns,
        )


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


def _validate_path_text(path: Path) -> None:
    try:
        os.fsencode(path)
    except UnicodeError as error:
        raise ReplayError("the replay path is not safely encodable") from error
    if any(
        unicodedata.category(character).startswith("C")
        for component in path.parts
        for character in component
    ):
        raise ReplayError("the replay path contains a control or format character")
    if not path.name or path.name in {".", ".."} or path.suffix.lower() != ".json":
        raise ReplayError("the replay path must name a .json file")


def _open_parent_without_symlinks(path: Path) -> int:
    parent = path.parent
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    if parent.is_absolute():
        descriptor = os.open("/", flags)
        components = parent.parts[1:]
    else:
        descriptor = os.open(".", flags)
        components = parent.parts

    try:
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                raise ReplayError("parent traversal is not accepted in replay paths")
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            try:
                os.close(descriptor)
            except OSError as error:
                with suppress(OSError):
                    os.close(next_descriptor)
                raise ReplayError(
                    "could not safely advance through the replay parent"
                ) from error
            descriptor = next_descriptor
        return descriptor
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _read_manifest_bytes(path: Path) -> bytes:
    _validate_path_text(path)
    try:
        parent_descriptor = _open_parent_without_symlinks(path)
    except (OSError, UnicodeError) as error:
        raise ReplayError("could not safely open the replay parent") from error

    file_descriptor = -1
    try:
        parent_before = _Identity.from_stat(os.fstat(parent_descriptor))
        enumerated = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        expected = _Identity.from_stat(enumerated)
        if (
            not stat.S_ISREG(enumerated.st_mode)
            or enumerated.st_nlink != 1
            or enumerated.st_size < 1
            or enumerated.st_size > MAX_REPLAY_BYTES
        ):
            raise ReplayError(
                "the replay manifest must be a bounded single-link regular file"
            )

        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        file_descriptor = os.open(
            path.name,
            flags,
            dir_fd=parent_descriptor,
        )
        opened = _Identity.from_stat(os.fstat(file_descriptor))
        if opened != expected:
            raise ReplayError("the replay manifest changed before it was opened")

        chunks: list[bytes] = []
        remaining = MAX_REPLAY_BYTES + 1
        while remaining:
            chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != opened.size or len(data) > MAX_REPLAY_BYTES:
            raise ReplayError("the replay manifest changed while it was read")
        if _Identity.from_stat(os.fstat(file_descriptor)) != opened:
            raise ReplayError("the replay manifest changed while it was read")

        named_after = _Identity.from_stat(
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        parent_after = _Identity.from_stat(os.fstat(parent_descriptor))
        if named_after != expected or parent_after != parent_before:
            raise ReplayError("the replay path changed during validation")
        return data
    except ReplayError:
        raise
    except (OSError, UnicodeError) as error:
        raise ReplayError("could not safely read the replay manifest") from error
    finally:
        close_failure: OSError | None = None
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError as error:
                close_failure = error
        try:
            os.close(parent_descriptor)
        except OSError as error:
            close_failure = close_failure or error
        if close_failure is not None:
            raise ReplayError(
                "could not safely close the replay manifest"
            ) from close_failure


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError("the replay manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ReplayError("the replay manifest contains a non-finite JSON value")


def load_manifest(path: Path) -> tuple[ReplayManifest, str]:
    """Safely load and validate a replay manifest and its raw-file digest."""
    data = _read_manifest_bytes(path)
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ReplayError("the replay manifest must be strict UTF-8") from error
    if text.startswith("\ufeff"):
        raise ReplayError("the replay manifest must not contain a UTF-8 BOM")

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        validated_json = json.dumps(
            decoded,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        manifest = ReplayManifest.model_validate_json(validated_json, strict=True)
    except ReplayError:
        raise
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
