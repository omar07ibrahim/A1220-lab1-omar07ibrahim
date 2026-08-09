from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import receipt_extractor.replay as replay
from receipt_extractor.artifact_io import ArtifactIOErrorCode
from receipt_extractor.file_io import DEFAULT_MAX_FILE_BYTES, ImagePayload
from receipt_extractor.replay import (
    MAX_REPLAY_BYTES,
    ReplayError,
    ReplayInputDescriptor,
    ReplayProvider,
    batch_digest,
    descriptor_for,
    load_manifest,
)
from receipt_extractor.schema import ExpenseCategory


def _image(
    name: str,
    data: bytes,
    *,
    width: int = 3,
    height: int = 2,
    media_type: str = "image/png",
) -> ImagePayload:
    return ImagePayload(
        name=name,
        media_type=media_type,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
    )


def _output(
    vendor: str,
    *,
    category: str | None = "Meals",
) -> dict[str, object]:
    return {
        "date": "2026-07-24",
        "amount": "$12.34",
        "vendor": vendor,
        "category": category,
    }


def _document(
    images: Sequence[ImagePayload],
    outputs: Sequence[dict[str, object]] | None = None,
) -> dict[str, Any]:
    descriptors = [descriptor_for(image) for image in images]
    selected_outputs = (
        list(outputs)
        if outputs is not None
        else [_output(f"Vendor {index}") for index, _ in enumerate(images, start=1)]
    )
    assert len(selected_outputs) == len(descriptors)
    return {
        "kind": "receipt-extractor-replay",
        "schema_version": 1,
        "batch": {
            "digest": batch_digest(descriptors),
            "items": [
                {
                    "input": descriptor.model_dump(mode="json"),
                    "output": output,
                }
                for descriptor, output in zip(
                    descriptors,
                    selected_outputs,
                    strict=True,
                )
            ],
        },
    }


def _write_manifest(path: Path, document: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    path.write_bytes(encoded)
    return encoded


def _manifest_images() -> list[ImagePayload]:
    return [
        _image("a.png", b"synthetic-image-a"),
        _image("b.png", b"synthetic-image-b", width=4, height=5),
    ]


def test_valid_manifest_round_trips_typed_outputs_and_raw_digest(
    tmp_path: Path,
) -> None:
    [image, *_] = _manifest_images()
    document = _document([image], [_output("ACME", category="Meals")])
    path = tmp_path / "receipt-replay.json"
    encoded = _write_manifest(path, document)

    manifest, raw_digest = load_manifest(path)

    assert manifest.kind == "receipt-extractor-replay"
    assert manifest.schema_version == 1
    assert manifest.batch.items[0].input == descriptor_for(image)
    assert manifest.batch.items[0].output.vendor == "ACME"
    assert manifest.batch.items[0].output.category is ExpenseCategory.MEALS
    assert raw_digest == f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_batch_digest_has_exact_domain_separated_canonical_form() -> None:
    image = _image("receipt.png", b"receipt-bytes")
    descriptor = descriptor_for(image)
    canonical = json.dumps(
        {
            "images": [
                {
                    "height": 2,
                    "media_type": "image/png",
                    "name": "receipt.png",
                    "sha256": hashlib.sha256(b"receipt-bytes").hexdigest(),
                    "size_bytes": len(b"receipt-bytes"),
                    "width": 3,
                }
            ]
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    expected = hashlib.sha256(
        b"auditable-receipt-extractor/replay-batch/v1\0" + canonical
    ).hexdigest()

    assert batch_digest([descriptor]) == f"sha256:{expected}"
    assert batch_digest([descriptor]) == batch_digest([descriptor])


def test_batch_digest_is_order_and_descriptor_sensitive() -> None:
    images = _manifest_images()
    first = [descriptor_for(image) for image in images]
    changed_name = first.copy()
    changed_name[0] = first[0].model_copy(update={"name": "renamed.png"})
    changed_content = first.copy()
    changed_content[0] = first[0].model_copy(
        update={"sha256": hashlib.sha256(b"different").hexdigest()}
    )
    changed_dimensions = first.copy()
    changed_dimensions[0] = first[0].model_copy(update={"width": 4})

    baseline = batch_digest(first)

    assert batch_digest(list(reversed(first))) != baseline
    assert batch_digest(changed_name) != baseline
    assert batch_digest(changed_content) != baseline
    assert batch_digest(changed_dimensions) != baseline


@pytest.mark.parametrize("change", ["order", "name", "content", "dimensions"])
def test_bind_rejects_every_exact_batch_mismatch(
    tmp_path: Path,
    change: str,
) -> None:
    expected = _manifest_images()
    path = tmp_path / f"{change}.json"
    _write_manifest(path, _document(expected))
    actual = expected.copy()
    if change == "order":
        actual.reverse()
    elif change == "name":
        actual[0] = _image("renamed.png", actual[0].data)
    elif change == "content":
        actual[0] = _image(actual[0].name, b"SYNTHETIC-IMAGE-A")
    else:
        actual[0] = _image(
            actual[0].name,
            actual[0].data,
            width=actual[0].width + 1,
            height=actual[0].height,
        )

    with pytest.raises(ReplayError, match="does not match the input batch"):
        ReplayProvider.bind(path, actual)


def test_manifest_rejects_digest_that_does_not_cover_its_items(
    tmp_path: Path,
) -> None:
    images = _manifest_images()
    document = _document(images)
    original = document["batch"]["digest"]
    assert isinstance(original, str)
    replacement = "0" if original[-1] != "0" else "1"
    document["batch"]["digest"] = f"{original[:-1]}{replacement}"
    path = tmp_path / "bad-digest.json"
    _write_manifest(path, document)

    with pytest.raises(ReplayError, match="digest does not match"):
        load_manifest(path)


def test_duplicate_names_are_rejected_even_with_a_valid_digest(
    tmp_path: Path,
) -> None:
    images = [
        _image("same.png", b"first"),
        _image("same.png", b"second"),
    ]
    path = tmp_path / "duplicate-name.json"
    _write_manifest(path, _document(images))

    with pytest.raises(ReplayError, match="schema v1"):
        load_manifest(path)


def test_duplicate_content_hashes_are_allowed_for_distinct_names(
    tmp_path: Path,
) -> None:
    images = [
        _image("copy-a.png", b"same-content"),
        _image("copy-b.png", b"same-content"),
    ]
    outputs = [_output("First"), _output("Second", category="Other")]
    path = tmp_path / "duplicate-content.json"
    _write_manifest(path, _document(images, outputs))

    provider = ReplayProvider.bind(path, images)

    assert provider(images[0])["vendor"] == "First"
    assert provider(images[1])["vendor"] == "Second"
    provider.finalize()


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        (
            "duplicate-key",
            (b'{"kind":"receipt-extractor-replay","kind":"receipt-extractor-replay"}'),
            "duplicate JSON key",
        ),
        ("bom", b"\xef\xbb\xbf{}", "UTF-8 BOM"),
        ("invalid-utf8", b'{"kind":"\xff"}', "strict UTF-8"),
        ("nan", b'{"kind":NaN}', "non-finite JSON"),
        ("infinity", b'{"kind":Infinity}', "non-finite JSON"),
    ],
)
def test_noncanonical_json_encodings_fail_closed(
    tmp_path: Path,
    name: str,
    raw: bytes,
    message: str,
) -> None:
    path = tmp_path / f"{name}.json"
    path.write_bytes(raw)

    with pytest.raises(ReplayError, match=message):
        load_manifest(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-top-level",
        "missing-kind",
        "coerced-version",
        "extra-input",
        "missing-width",
        "coerced-size",
        "extra-output",
        "missing-output-field",
        "numeric-amount",
        "unknown-category",
        "pixel-product-limit",
        "invalid-digest-format",
    ],
)
def test_schema_rejects_extra_missing_and_coerced_values(
    tmp_path: Path,
    mutation: str,
) -> None:
    [image, *_] = _manifest_images()
    document = copy.deepcopy(_document([image]))
    item = document["batch"]["items"][0]
    input_descriptor = item["input"]
    output = item["output"]

    if mutation == "extra-top-level":
        document["unexpected"] = True
    elif mutation == "missing-kind":
        del document["kind"]
    elif mutation == "coerced-version":
        document["schema_version"] = "1"
    elif mutation == "extra-input":
        input_descriptor["path"] = "/private/receipt.png"
    elif mutation == "missing-width":
        del input_descriptor["width"]
    elif mutation == "coerced-size":
        input_descriptor["size_bytes"] = str(input_descriptor["size_bytes"])
    elif mutation == "extra-output":
        output["currency"] = "USD"
    elif mutation == "missing-output-field":
        del output["vendor"]
    elif mutation == "numeric-amount":
        output["amount"] = 12.34
    elif mutation == "unknown-category":
        output["category"] = "Travel"
    elif mutation == "pixel-product-limit":
        input_descriptor["width"] = 25_000_000
        input_descriptor["height"] = 2
    else:
        document["batch"]["digest"] = "sha256:not-a-digest"

    path = tmp_path / f"{mutation}.json"
    _write_manifest(path, document)

    with pytest.raises(ReplayError, match="schema v1"):
        load_manifest(path)


def test_json_root_must_be_the_manifest_object(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ReplayError, match="schema v1"):
        load_manifest(path)


def test_final_symlink_is_rejected(tmp_path: Path) -> None:
    [image, *_] = _manifest_images()
    target = tmp_path / "target.json"
    _write_manifest(target, _document([image]))
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(ReplayError, match="single-link regular file"):
        load_manifest(link)


def test_symlink_in_parent_path_is_rejected(tmp_path: Path) -> None:
    [image, *_] = _manifest_images()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "manifest.json"
    _write_manifest(target, _document([image]))
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ReplayError, match="safely open the replay parent"):
        load_manifest(linked_parent / target.name)


def test_hard_linked_manifest_is_rejected(tmp_path: Path) -> None:
    [image, *_] = _manifest_images()
    target = tmp_path / "target.json"
    _write_manifest(target, _document([image]))
    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)

    with pytest.raises(ReplayError, match="single-link regular file"):
        load_manifest(hardlink)


def test_fifo_manifest_is_rejected_without_opening_it(tmp_path: Path) -> None:
    fifo = tmp_path / "manifest.json"
    os.mkfifo(fifo)

    with pytest.raises(ReplayError, match="single-link regular file"):
        load_manifest(fifo)


@pytest.mark.parametrize("size", [0, MAX_REPLAY_BYTES + 1])
def test_empty_and_oversized_manifests_are_rejected(
    tmp_path: Path,
    size: int,
) -> None:
    path = tmp_path / f"size-{size}.json"
    with path.open("wb") as stream:
        stream.truncate(size)

    with pytest.raises(ReplayError, match="bounded single-link regular file"):
        load_manifest(path)


def test_invalid_internal_size_limit_is_a_stable_replay_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(replay, "MAX_REPLAY_BYTES", True)

    with pytest.raises(ReplayError) as captured:
        load_manifest(path)

    assert str(captured.value) == "the replay manifest size limit is invalid"


def test_every_shared_artifact_error_has_a_replay_translation() -> None:
    assert set(replay._ARTIFACT_ERROR_MESSAGES) == set(ArtifactIOErrorCode)


def test_suffix_and_parent_traversal_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_suffix = tmp_path / "manifest.txt"
    wrong_suffix.write_text("{}", encoding="utf-8")
    with pytest.raises(ReplayError, match=r"must name a \.json"):
        load_manifest(wrong_suffix)

    parent_manifest = tmp_path / "manifest.json"
    parent_manifest.write_text("{}", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ReplayError, match="parent traversal"):
        load_manifest(Path("../manifest.json"))


def test_file_swap_between_stat_and_open_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [image, *_] = _manifest_images()
    target = tmp_path / "manifest.json"
    replacement = tmp_path / "replacement.json"
    _write_manifest(target, _document([image]))
    _write_manifest(replacement, _document([_image("other.png", b"other")]))
    real_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if dir_fd is not None and path == target.name and not swapped:
            os.replace(replacement, target)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(ReplayError, match="changed before it was opened"):
        load_manifest(target)
    assert swapped


def test_same_inode_mutation_during_read_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [image, *_] = _manifest_images()
    target = tmp_path / "manifest.json"
    original = _write_manifest(target, _document([image], [_output("Store")]))
    mutated = original.replace(b'"Store"', b'"St0re"')
    assert mutated != original
    assert len(mutated) == len(original)
    real_read = os.read
    changed = False

    def mutate_before_read(descriptor: int, length: int) -> bytes:
        nonlocal changed
        if not changed:
            target.write_bytes(mutated)
            changed = True
        return real_read(descriptor, length)

    monkeypatch.setattr(os, "read", mutate_before_read)

    with pytest.raises(ReplayError, match="changed while it was read"):
        load_manifest(target)
    assert changed


def test_provider_enforces_cursor_order_completeness_and_single_use(
    tmp_path: Path,
) -> None:
    images = _manifest_images()
    outputs = [_output("First"), _output("Second", category=None)]
    path = tmp_path / "provider.json"
    encoded = _write_manifest(path, _document(images, outputs))
    provider = ReplayProvider.bind(path, images)

    assert provider.manifest_sha256 == (f"sha256:{hashlib.sha256(encoded).hexdigest()}")
    with pytest.raises(ReplayError, match="not consumed completely"):
        provider.finalize()

    assert provider(images[0]) == outputs[0]
    with pytest.raises(ReplayError, match="input order changed"):
        provider(images[0])
    assert provider(images[1]) == outputs[1]
    provider.finalize()

    with pytest.raises(ReplayError, match="consumed more than once"):
        provider(images[0])


def test_descriptor_contract_rejects_unsafe_or_inconsistent_metadata() -> None:
    valid = descriptor_for(_image("receipt.png", b"receipt"))
    assert (
        ReplayInputDescriptor.model_validate(
            valid.model_dump(mode="python"),
            strict=True,
        )
        == valid
    )

    for update in (
        {"name": "receipt\u202e.png"},
        {"name": f"{'x' * 256}.png"},
        {"media_type": "image/gif"},
        {"size_bytes": 0},
        {"sha256": "A" * 64},
        {"width": 25_000_000, "height": 2},
    ):
        with pytest.raises(ValueError):
            ReplayInputDescriptor.model_validate(
                valid.model_copy(update=update).model_dump(mode="python"),
                strict=True,
            )


def test_descriptor_supports_caller_bounded_files_above_the_default() -> None:
    image = _image(
        "large.png",
        b"x" * (DEFAULT_MAX_FILE_BYTES + 1),
    )

    descriptor = descriptor_for(image)

    assert descriptor.size_bytes == DEFAULT_MAX_FILE_BYTES + 1


def test_descriptor_wraps_unrepresentable_validated_metadata() -> None:
    image = _image("receipt.gif", b"gif", media_type="image/gif")

    with pytest.raises(
        ReplayError,
        match="cannot be represented by replay schema v1",
    ):
        descriptor_for(image)
