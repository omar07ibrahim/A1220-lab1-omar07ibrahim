"""Descriptor-pinned, bounded loading for local JSON artifacts."""

from __future__ import annotations

import json
import math
import os
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

MAX_JSON_ARTIFACT_BYTES = 16 * 1024 * 1024


class ArtifactIOErrorCode(StrEnum):
    """Stable, non-sensitive failure codes for artifact consumers."""

    INVALID_SIZE_LIMIT = "invalid_size_limit"
    PATH_NOT_ENCODABLE = "path_not_encodable"
    PATH_CONTROL_CHARACTER = "path_control_character"
    JSON_PATH_REQUIRED = "json_path_required"
    PARENT_TRAVERSAL = "parent_traversal"
    PARENT_ADVANCE_FAILED = "parent_advance_failed"
    PARENT_OPEN_FAILED = "parent_open_failed"
    BOUNDED_REGULAR_FILE_REQUIRED = "bounded_regular_file_required"
    FILE_CHANGED_BEFORE_OPEN = "file_changed_before_open"
    FILE_CHANGED_DURING_READ = "file_changed_during_read"
    PATH_CHANGED_DURING_VALIDATION = "path_changed_during_validation"
    READ_FAILED = "read_failed"
    CLOSE_FAILED = "close_failed"
    INVALID_UTF8 = "invalid_utf8"
    UTF8_BOM = "utf8_bom"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    NONFINITE_JSON_VALUE = "nonfinite_json_value"
    INVALID_JSON = "invalid_json"


class ArtifactIOError(ValueError):
    """Raised with a stable code when an artifact cannot be loaded safely."""

    def __init__(self, code: ArtifactIOErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class JsonArtifact:
    """Raw bytes and strictly decoded value from one pinned JSON artifact."""

    raw_bytes: bytes
    value: Any


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


def _fail(code: ArtifactIOErrorCode) -> NoReturn:
    raise ArtifactIOError(code)


def _validate_path_text(path: Path) -> None:
    try:
        os.fsencode(path)
    except UnicodeError as error:
        raise ArtifactIOError(ArtifactIOErrorCode.PATH_NOT_ENCODABLE) from error
    if any(
        unicodedata.category(character).startswith("C")
        for component in path.parts
        for character in component
    ):
        _fail(ArtifactIOErrorCode.PATH_CONTROL_CHARACTER)
    if not path.name or path.name in {".", ".."} or path.suffix.lower() != ".json":
        _fail(ArtifactIOErrorCode.JSON_PATH_REQUIRED)


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
                _fail(ArtifactIOErrorCode.PARENT_TRAVERSAL)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                os.close(descriptor)
            except OSError as error:
                with suppress(OSError):
                    os.close(next_descriptor)
                raise ArtifactIOError(
                    ArtifactIOErrorCode.PARENT_ADVANCE_FAILED
                ) from error
            descriptor = next_descriptor
        return descriptor
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
    _validate_path_text(path)
    try:
        parent_descriptor = _open_parent_without_symlinks(path)
    except ArtifactIOError:
        raise
    except (OSError, UnicodeError) as error:
        raise ArtifactIOError(ArtifactIOErrorCode.PARENT_OPEN_FAILED) from error

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
            or enumerated.st_size > max_bytes
        ):
            _fail(ArtifactIOErrorCode.BOUNDED_REGULAR_FILE_REQUIRED)

        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        file_descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = _Identity.from_stat(os.fstat(file_descriptor))
        if opened != expected:
            _fail(ArtifactIOErrorCode.FILE_CHANGED_BEFORE_OPEN)

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != opened.size or len(data) > max_bytes:
            _fail(ArtifactIOErrorCode.FILE_CHANGED_DURING_READ)
        if _Identity.from_stat(os.fstat(file_descriptor)) != opened:
            _fail(ArtifactIOErrorCode.FILE_CHANGED_DURING_READ)

        named_after = _Identity.from_stat(
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        parent_after = _Identity.from_stat(os.fstat(parent_descriptor))
        if named_after != expected or parent_after != parent_before:
            _fail(ArtifactIOErrorCode.PATH_CHANGED_DURING_VALIDATION)
        return data
    except ArtifactIOError:
        raise
    except (OSError, UnicodeError) as error:
        raise ArtifactIOError(ArtifactIOErrorCode.READ_FAILED) from error
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
            raise ArtifactIOError(ArtifactIOErrorCode.CLOSE_FAILED) from close_failure


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(ArtifactIOErrorCode.DUPLICATE_JSON_KEY)
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    _fail(ArtifactIOErrorCode.NONFINITE_JSON_VALUE)


def _parse_finite_float(value: str) -> float:
    decoded = float(value)
    if not math.isfinite(decoded):
        _fail(ArtifactIOErrorCode.NONFINITE_JSON_VALUE)
    return decoded


def load_json_artifact(path: Path, *, max_bytes: int) -> JsonArtifact:
    """Load one non-empty, bounded JSON file without following path links."""
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_JSON_ARTIFACT_BYTES
    ):
        _fail(ArtifactIOErrorCode.INVALID_SIZE_LIMIT)

    data = _read_bounded_bytes(path, max_bytes=max_bytes)
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ArtifactIOError(ArtifactIOErrorCode.INVALID_UTF8) from error
    if text.startswith("\ufeff"):
        _fail(ArtifactIOErrorCode.UTF8_BOM)

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except ArtifactIOError:
        raise
    except (RecursionError, UnicodeError, ValueError) as error:
        raise ArtifactIOError(ArtifactIOErrorCode.INVALID_JSON) from error
    return JsonArtifact(raw_bytes=data, value=value)
