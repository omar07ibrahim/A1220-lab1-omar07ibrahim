from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from receipt_extractor.artifact_io import (
    MAX_JSON_ARTIFACT_BYTES,
    ArtifactIOError,
    ArtifactIOErrorCode,
    load_json_artifact,
)


def test_load_json_artifact_returns_exact_bytes_and_strict_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact.json"
    raw = b'{"items":[1,true,null],"name":"synthetic"}'
    path.write_bytes(raw)

    artifact = load_json_artifact(path, max_bytes=len(raw))

    assert artifact.raw_bytes == raw
    assert artifact.value == {
        "items": [1, True, None],
        "name": "synthetic",
    }


@pytest.mark.parametrize(
    ("name", "raw", "code"),
    [
        (
            "invalid-utf8.json",
            b'{"value":"\xff"}',
            ArtifactIOErrorCode.INVALID_UTF8,
        ),
        (
            "bom.json",
            b"\xef\xbb\xbf{}",
            ArtifactIOErrorCode.UTF8_BOM,
        ),
        (
            "duplicate.json",
            b'{"value":1,"value":2}',
            ArtifactIOErrorCode.DUPLICATE_JSON_KEY,
        ),
        (
            "nan.json",
            b'{"value":NaN}',
            ArtifactIOErrorCode.NONFINITE_JSON_VALUE,
        ),
        (
            "exponent-overflow.json",
            b'{"value":1e9999}',
            ArtifactIOErrorCode.NONFINITE_JSON_VALUE,
        ),
        (
            "malformed.json",
            b'{"value":',
            ArtifactIOErrorCode.INVALID_JSON,
        ),
    ],
)
def test_json_decode_failures_have_stable_codes(
    tmp_path: Path,
    name: str,
    raw: bytes,
    code: ArtifactIOErrorCode,
) -> None:
    path = tmp_path / name
    path.write_bytes(raw)

    with pytest.raises(ArtifactIOError) as captured:
        load_json_artifact(path, max_bytes=1024)

    assert captured.value.code is code
    assert str(captured.value) == code.value


@pytest.mark.parametrize(
    "invalid_limit",
    [
        True,
        False,
        0,
        -1,
        1.5,
        "1024",
        MAX_JSON_ARTIFACT_BYTES + 1,
    ],
)
def test_invalid_size_limits_have_a_stable_code(
    tmp_path: Path,
    invalid_limit: object,
) -> None:
    path = tmp_path / "artifact.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactIOError) as captured:
        load_json_artifact(path, max_bytes=cast(int, invalid_limit))

    assert captured.value.code is ArtifactIOErrorCode.INVALID_SIZE_LIMIT


def test_path_and_file_failures_have_stable_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_suffix = tmp_path / "artifact.txt"
    wrong_suffix.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactIOError) as suffix_failure:
        load_json_artifact(wrong_suffix, max_bytes=1024)
    assert suffix_failure.value.code is ArtifactIOErrorCode.JSON_PATH_REQUIRED

    empty = tmp_path / "empty.json"
    empty.touch()
    with pytest.raises(ArtifactIOError) as empty_failure:
        load_json_artifact(empty, max_bytes=1024)
    assert empty_failure.value.code is ArtifactIOErrorCode.BOUNDED_REGULAR_FILE_REQUIRED

    parent_artifact = tmp_path / "artifact.json"
    parent_artifact.write_text("{}", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ArtifactIOError) as traversal_failure:
        load_json_artifact(Path("../artifact.json"), max_bytes=1024)
    assert traversal_failure.value.code is ArtifactIOErrorCode.PARENT_TRAVERSAL


def test_final_symlink_is_rejected_with_a_stable_code(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    os.symlink(target, link)

    with pytest.raises(ArtifactIOError) as captured:
        load_json_artifact(link, max_bytes=1024)

    assert captured.value.code is ArtifactIOErrorCode.BOUNDED_REGULAR_FILE_REQUIRED
