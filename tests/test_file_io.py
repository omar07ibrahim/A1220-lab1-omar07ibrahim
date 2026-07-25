from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from receipt_extractor import file_io
from tests.conftest import ImageFactory


@pytest.mark.parametrize(
    ("name", "image_format", "media_type"),
    [
        ("receipt.jpg", "JPEG", "image/jpeg"),
        ("receipt.png", "PNG", "image/png"),
        ("receipt.webp", "WEBP", "image/webp"),
    ],
)
def test_real_formats_are_decoded_and_data_urls_match(
    tmp_path: Path,
    image_factory: ImageFactory,
    name: str,
    image_format: str,
    media_type: str,
) -> None:
    directory = tmp_path / image_format.lower()
    directory.mkdir()
    expected = image_factory(directory / name, image_format)

    [payload] = file_io.load_images(directory)

    assert payload.name == name
    assert payload.media_type == media_type
    assert payload.data == expected
    assert payload.sha256 == hashlib.sha256(expected).hexdigest()
    assert (payload.width, payload.height) == (3, 2)
    header, encoded = payload.data_url().split(",", maxsplit=1)
    assert header == f"data:{media_type};base64"
    assert base64.b64decode(encoded, validate=True) == expected


def test_order_and_audit_metadata_are_deterministic(
    tmp_path: Path,
    image_factory: ImageFactory,
) -> None:
    directory = tmp_path / "ordered"
    directory.mkdir()
    image_factory(directory / "b.png", "PNG", color="blue")
    image_factory(directory / "A.png", "PNG", color="red")
    image_factory(directory / "a.png", "PNG", color="green")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    (directory / "nested").mkdir()

    payloads = file_io.load_images(directory)

    assert [payload.name for payload in payloads] == ["A.png", "a.png", "b.png"]
    assert [payload.audit_metadata()["name"] for payload in payloads] == [
        "A.png",
        "a.png",
        "b.png",
    ]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("broken.png", b"\x89PNG\r\n\x1a\njunk"),
        ("broken.jpg", b"\xff\xd8\xffjunk\xff\xd9"),
        ("broken.webp", b"RIFF\x04\x00\x00\x00WEBP"),
    ],
)
def test_header_only_images_are_rejected(
    tmp_path: Path,
    name: str,
    data: bytes,
) -> None:
    directory = tmp_path / "corrupt"
    directory.mkdir()
    (directory / name).write_bytes(data)

    with pytest.raises(file_io.ImageInputError, match=r"decoded|container"):
        file_io.load_images(directory)


@pytest.mark.parametrize(
    ("name", "image_format"),
    [
        ("receipt.jpg", "JPEG"),
        ("receipt.png", "PNG"),
        ("receipt.webp", "WEBP"),
    ],
)
def test_truncation_and_appended_payloads_are_rejected(
    tmp_path: Path,
    image_factory: ImageFactory,
    name: str,
    image_format: str,
) -> None:
    truncated_dir = tmp_path / f"truncated-{image_format}"
    appended_dir = tmp_path / f"appended-{image_format}"
    truncated_dir.mkdir()
    appended_dir.mkdir()
    valid = image_factory(truncated_dir / name, image_format)
    (truncated_dir / name).write_bytes(valid[:-2])
    (appended_dir / name).write_bytes(valid + b"PK\x03\x04synthetic-payload")

    with pytest.raises(file_io.ImageInputError):
        file_io.load_images(truncated_dir)
    with pytest.raises(file_io.ImageInputError, match="container"):
        file_io.load_images(appended_dir)


@pytest.mark.parametrize(
    ("name", "image_format", "duplicate_trailer"),
    [
        ("receipt.jpg", "JPEG", b"\xff\xd9"),
        (
            "receipt.png",
            "PNG",
            b"\x00\x00\x00\x00IEND\xaeB`\x82",
        ),
    ],
)
def test_appended_payload_with_duplicate_trailer_is_rejected(
    tmp_path: Path,
    image_factory: ImageFactory,
    name: str,
    image_format: str,
    duplicate_trailer: bytes,
) -> None:
    directory = tmp_path / f"duplicate-{image_format}"
    directory.mkdir()
    valid = image_factory(directory / name, image_format)
    (directory / name).write_bytes(
        valid + b"PK\x03\x04hidden-payload" + duplicate_trailer
    )

    with pytest.raises(file_io.ImageInputError, match="container"):
        file_io.load_images(directory)


def test_container_boundary_parsers_fail_closed_on_malformed_structures() -> None:
    png_signature = b"\x89PNG\r\n\x1a\n"
    assert not file_io._png_has_exact_container_bounds(png_signature)
    assert not file_io._png_has_exact_container_bounds(png_signature + b"\x00\x00\x00")
    assert not file_io._png_has_exact_container_bounds(
        png_signature + b"\x00\x00\x00\x10IENDshort"
    )
    assert not file_io._png_has_exact_container_bounds(
        png_signature + b"\x00\x00\x00\x01IENDx\x00\x00\x00\x00"
    )
    assert not file_io._png_has_exact_container_bounds(
        png_signature + b"\x00\x00\x00\x00IDAT\x00\x00\x00\x00"
    )

    assert not file_io._jpeg_has_exact_container_bounds(b"not-jpeg")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8data")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff\xff")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff\x00")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff\xe0\x00")
    assert not file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff\xe0\x00\x01")
    assert not file_io._jpeg_has_exact_container_bounds(
        b"\xff\xd8\xff\xe0\x00\x10short"
    )
    assert file_io._jpeg_has_exact_container_bounds(b"\xff\xd8\xff\xd8\xff\xd9")

    assert not file_io._webp_has_exact_container_bounds(b"RIFF")
    assert not file_io._webp_has_exact_container_bounds(b"RIFF\x05\x00\x00\x00WEBP")
    assert not file_io._webp_has_exact_container_bounds(b"RIFF\x07\x00\x00\x00WEBPabc")
    assert not file_io._webp_has_exact_container_bounds(
        b"RIFF\x0c\x00\x00\x00WEBPJUNK\x10\x00\x00\x00"
    )
    assert file_io._webp_has_exact_container_bounds(
        b"RIFF\x0e\x00\x00\x00WEBPJUNK\x01\x00\x00\x00x\x00"
    )
    assert not file_io._has_exact_container_bounds(b"", "image/unknown")


def test_extension_mismatch_and_decompression_bomb_are_rejected(
    tmp_path: Path,
    image_factory: ImageFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    image_factory(mismatch / "receipt.jpg", "PNG")
    with pytest.raises(file_io.ImageInputError, match="extension"):
        file_io.load_images(mismatch)

    bomb = tmp_path / "bomb"
    bomb.mkdir()
    image_factory(bomb / "receipt.png", "PNG", size=(2, 2))
    monkeypatch.setattr(file_io, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(file_io.ImageInputError, match="decoded safely"):
        file_io.load_images(bomb)


def test_exact_file_count_and_byte_limits(
    tmp_path: Path,
    image_factory: ImageFactory,
) -> None:
    directory = tmp_path / "limits"
    directory.mkdir()
    first = image_factory(directory / "a.png", "PNG", color="red")
    second = image_factory(directory / "b.png", "PNG", color="blue")

    assert (
        len(
            file_io.load_images(
                directory,
                max_files=2,
                max_file_bytes=max(len(first), len(second)),
                max_total_bytes=len(first) + len(second),
            )
        )
        == 2
    )
    with pytest.raises(file_io.ImageInputError, match="file-count"):
        file_io.load_images(directory, max_files=1)
    with pytest.raises(file_io.ImageInputError, match="per-file"):
        file_io.load_images(directory, max_file_bytes=max(len(first), len(second)) - 1)
    with pytest.raises(file_io.ImageInputError, match="total byte"):
        file_io.load_images(directory, max_total_bytes=len(first) + len(second) - 1)
    for kwargs in (
        {"max_files": 0},
        {"max_file_bytes": 0},
        {"max_total_bytes": 0},
    ):
        with pytest.raises(file_io.ImageInputError, match="at least 1"):
            file_io.load_images(directory, **kwargs)


def test_symlinks_hardlinks_and_unsafe_names_fail_closed(
    tmp_path: Path,
    image_factory: ImageFactory,
) -> None:
    source = tmp_path / "source.png"
    image_factory(source, "PNG")

    symlink_dir = tmp_path / "symlinks"
    symlink_dir.mkdir()
    (symlink_dir / "receipt.png").symlink_to(source)
    with pytest.raises(file_io.ImageInputError, match="symbolic"):
        file_io.load_images(symlink_dir)

    hardlink_dir = tmp_path / "hardlinks"
    hardlink_dir.mkdir()
    os.link(source, hardlink_dir / "receipt.png")
    with pytest.raises(file_io.ImageInputError, match="single-link"):
        file_io.load_images(hardlink_dir)

    unsafe_name_dir = tmp_path / "unsafe-name"
    unsafe_name_dir.mkdir()
    image_factory(unsafe_name_dir / "receipt\u202e.png", "PNG")
    with pytest.raises(file_io.ImageInputError, match="control or format"):
        file_io.load_images(unsafe_name_dir)

    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(symlink_dir, target_is_directory=True)
    with pytest.raises(file_io.ImageInputError, match="non-symlink"):
        file_io.load_images(directory_link)


@pytest.mark.parametrize("replacement_kind", ["regular", "fifo"])
def test_directory_fd_swap_is_detected_without_blocking(
    tmp_path: Path,
    image_factory: ImageFactory,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    directory = tmp_path / replacement_kind
    directory.mkdir()
    target = directory / "receipt.png"
    image_factory(target, "PNG", color="red")
    replacement = directory / "replacement"
    if replacement_kind == "regular":
        image_factory(replacement, "PNG", color="blue")
    else:
        os.mkfifo(replacement)

    real_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if dir_fd is not None and path == target.name and not swapped:
            os.replace(replacement, target)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(file_io.ImageInputError, match="changed"):
        file_io.load_images(directory)
    assert swapped


def test_same_inode_mutation_is_detected_after_read(
    tmp_path: Path,
    image_factory: ImageFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "mutation"
    directory.mkdir()
    target = directory / "receipt.png"
    original = image_factory(target, "PNG", color="red")
    replacement_buffer = bytearray(original)
    replacement_buffer[len(replacement_buffer) // 2] ^= 1
    replacement = bytes(replacement_buffer)

    real_read = os.read
    changed = False

    def mutate_before_read(descriptor: int, length: int) -> bytes:
        nonlocal changed
        if not changed:
            target.write_bytes(replacement)
            changed = True
        return real_read(descriptor, length)

    monkeypatch.setattr(os, "read", mutate_before_read)
    with pytest.raises(file_io.ImageInputError, match="changed while"):
        file_io.load_images(directory)
    assert changed


def test_special_file_and_directory_entry_limits(
    tmp_path: Path,
    image_factory: ImageFactory,
) -> None:
    special = tmp_path / "special"
    special.mkdir()
    os.mkfifo(special / "ignored.png")
    with pytest.raises(file_io.ImageInputError, match="no supported"):
        file_io.load_images(special)

    crowded = tmp_path / "crowded"
    crowded.mkdir()
    image_factory(crowded / "receipt.png", "PNG")
    for index in range(file_io.MAX_DIRECTORY_ENTRIES):
        (crowded / f"{index:04d}.txt").touch()
    with pytest.raises(file_io.ImageInputError, match="entry-count"):
        file_io.load_images(crowded)


def test_directory_descriptor_is_closed_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    before = len(list(Path("/proc/self/fd").iterdir()))
    real_fstat = os.fstat
    failed = False

    def fail_once(descriptor: int) -> os.stat_result:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fail_once)
    with pytest.raises(OSError, match="injected"):
        file_io._open_directory(directory)
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
