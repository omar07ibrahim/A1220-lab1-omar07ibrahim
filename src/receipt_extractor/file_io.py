"""Fail-closed image discovery and loading for receipt processing."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import unicodedata
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1_000
MAX_IMAGE_PIXELS = 25_000_000

_SUFFIXES_BY_MEDIA_TYPE = {
    "image/jpeg": {".jpeg", ".jpg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
_MEDIA_TYPE_BY_PIL_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_SUPPORTED_SUFFIXES = frozenset().union(*_SUFFIXES_BY_MEDIA_TYPE.values())


class ImageInputError(ValueError):
    """Raised when an input directory or image violates the upload contract."""


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """Validated image bytes and transport metadata."""

    name: str
    media_type: str
    data: bytes
    sha256: str
    width: int
    height: int

    @property
    def size_bytes(self) -> int:
        """Return the exact number of bytes that would be uploaded."""
        return len(self.data)

    def data_url(self) -> str:
        """Return a correctly typed base64 data URL for the provider."""
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def audit_metadata(self) -> dict[str, str | int]:
        """Return deterministic local audit metadata without paths or bytes."""
        return {
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, details: os.stat_result) -> _FileIdentity:
        return cls(
            device=details.st_dev,
            inode=details.st_ino,
            mode=details.st_mode,
            links=details.st_nlink,
            size=details.st_size,
            modified_ns=details.st_mtime_ns,
            changed_ns=details.st_ctime_ns,
        )


def _detect_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_has_exact_container_bounds(data: bytes) -> bool:
    offset = 8
    while offset < len(data):
        if offset + 12 > len(data):
            return False
        payload_size = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + payload_size
        if chunk_end > len(data):
            return False
        if chunk_type == b"IEND":
            return payload_size == 0 and chunk_end == len(data)
        offset = chunk_end
    return False


def _jpeg_has_exact_container_bounds(data: bytes) -> bool:
    if not data.startswith(b"\xff\xd8"):
        return False
    offset = 2
    in_scan = False
    while offset < len(data):
        marker_from_scan = in_scan
        if in_scan:
            marker_start = data.find(b"\xff", offset)
            if marker_start < 0:
                return False
            marker_offset = marker_start + 1
        else:
            if data[offset] != 0xFF:
                return False
            marker_offset = offset + 1

        while marker_offset < len(data) and data[marker_offset] == 0xFF:
            marker_offset += 1
        if marker_offset >= len(data):
            return False

        marker = data[marker_offset]
        offset = marker_offset + 1
        if marker_from_scan and marker == 0x00:
            continue
        if marker_from_scan and (marker == 0x01 or 0xD0 <= marker <= 0xD7):
            continue

        in_scan = False
        if marker == 0xD9:
            return offset == len(data)
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0x00 or offset + 2 > len(data):
            return False

        segment_size = int.from_bytes(data[offset : offset + 2], "big")
        if segment_size < 2 or offset + segment_size > len(data):
            return False
        offset += segment_size
        if marker == 0xDA:
            in_scan = True
    return False


def _webp_has_exact_container_bounds(data: bytes) -> bool:
    if (
        len(data) < 12
        or not data.startswith(b"RIFF")
        or data[8:12] != b"WEBP"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        return False

    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            return False
        payload_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        offset += 8 + payload_size + (payload_size & 1)
        if offset > len(data):
            return False
    return offset == len(data)


def _has_exact_container_bounds(data: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return _png_has_exact_container_bounds(data)
    if media_type == "image/jpeg":
        return _jpeg_has_exact_container_bounds(data)
    if media_type == "image/webp":
        return _webp_has_exact_container_bounds(data)
    return False


def _verify_image(data: bytes, media_type: str) -> tuple[int, int]:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise ImageInputError(
            "Pillow is required to decode and verify input images"
        ) from error

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                detected_media_type = _MEDIA_TYPE_BY_PIL_FORMAT.get(image.format or "")
                if detected_media_type != media_type:
                    raise ImageInputError(
                        "the decoded image format does not match its signature"
                    )
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or width * height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ImageInputError(
                        "input images must be single-frame and at most "
                        f"{MAX_IMAGE_PIXELS:,} pixels"
                    )
                image.verify()
            with Image.open(BytesIO(data)) as decoded:
                if (
                    _MEDIA_TYPE_BY_PIL_FORMAT.get(decoded.format or "") != media_type
                    or decoded.size != (width, height)
                    or getattr(decoded, "n_frames", 1) != 1
                ):
                    raise ImageInputError(
                        "the image changed interpretation during full decoding"
                    )
                decoded.load()
    except ImageInputError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise ImageInputError("an input image could not be decoded safely") from error

    if not _has_exact_container_bounds(data, media_type):
        raise ImageInputError(
            "an input image has trailing or incomplete container data"
        )
    return width, height


def _validate_limits(max_files: int, max_file_bytes: int, max_total_bytes: int) -> None:
    if max_files < 1:
        raise ImageInputError("max_files must be at least 1")
    if max_file_bytes < 1:
        raise ImageInputError("max_file_bytes must be at least 1")
    if max_total_bytes < 1:
        raise ImageInputError("max_total_bytes must be at least 1")


def _read_regular_file(
    directory_descriptor: int,
    name: str,
    enumerated: os.stat_result,
    max_file_bytes: int,
) -> bytes:
    expected = _FileIdentity.from_stat(enumerated)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise ImageInputError("could not safely open an input image") from error

    try:
        opened = os.fstat(descriptor)
        opened_identity = _FileIdentity.from_stat(opened)
        if opened_identity != expected:
            raise ImageInputError("an input image changed after directory enumeration")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ImageInputError("input images must be single-link regular files")
        if opened.st_size < 1:
            raise ImageInputError("empty image files are not accepted")
        if opened.st_size > max_file_bytes:
            raise ImageInputError("an input image exceeds the per-file size limit")

        chunks: list[bytes] = []
        remaining = max_file_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = _FileIdentity.from_stat(os.fstat(descriptor))
        if after != opened_identity or len(data) != opened.st_size:
            raise ImageInputError("an input image changed while it was being read")
        if len(data) > max_file_bytes:
            raise ImageInputError("an input image exceeds the per-file size limit")
        return data
    finally:
        os.close(descriptor)


def _validate_filename(name: str) -> None:
    if (
        not name
        or len(os.fsencode(name)) > 255
        or any(unicodedata.category(character).startswith("C") for character in name)
    ):
        raise ImageInputError(
            "input filenames must not contain control or format characters"
        )


def _open_directory(dirpath: str | os.PathLike[str]) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(os.fspath(dirpath), flags)
    except (OSError, UnicodeError) as error:
        raise ImageInputError(
            "the input path must be an existing, non-symlink directory"
        ) from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise ImageInputError("the input path must be a directory")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def load_images(
    dirpath: str | os.PathLike[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[ImagePayload]:
    """Load a deterministic, bounded set of direct child receipt images.

    Directories and unsupported regular files are ignored. Symbolic links,
    hard-linked images, misleading extensions, undecodable images, unsafe
    filenames, empty files, concurrent changes, and limit violations fail the
    complete preflight before any API request is made.
    """
    _validate_limits(max_files, max_file_bytes, max_total_bytes)
    directory_descriptor = _open_directory(dirpath)
    try:
        directory_before = _FileIdentity.from_stat(os.fstat(directory_descriptor))
        with os.scandir(directory_descriptor) as entries:
            snapshots: list[tuple[str, os.stat_result]] = []
            for entry in entries:
                _validate_filename(entry.name)
                try:
                    enumerated = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ImageInputError(
                        "an input entry changed during directory enumeration"
                    ) from error
                snapshots.append((entry.name, enumerated))
                if len(snapshots) > MAX_DIRECTORY_ENTRIES:
                    raise ImageInputError(
                        "the input directory exceeds the entry-count limit"
                    )
        ordered_entries = sorted(
            snapshots,
            key=lambda snapshot: (snapshot[0].casefold(), snapshot[0]),
        )

        images: list[ImagePayload] = []
        total_bytes = 0
        for name, enumerated in ordered_entries:
            if stat.S_ISLNK(enumerated.st_mode):
                raise ImageInputError("symbolic links are not accepted as inputs")
            if not stat.S_ISREG(enumerated.st_mode):
                continue

            suffix = Path(name).suffix.lower()
            if suffix not in _SUPPORTED_SUFFIXES:
                continue

            data = _read_regular_file(
                directory_descriptor,
                name,
                enumerated,
                max_file_bytes,
            )
            media_type = _detect_media_type(data)
            if media_type is None or suffix not in _SUFFIXES_BY_MEDIA_TYPE[media_type]:
                raise ImageInputError(
                    "an input extension does not match its image signature"
                )
            width, height = _verify_image(data, media_type)
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise ImageInputError("input images exceed the total byte limit")
            images.append(
                ImagePayload(
                    name=name,
                    media_type=media_type,
                    data=data,
                    sha256=hashlib.sha256(data).hexdigest(),
                    width=width,
                    height=height,
                )
            )
            if len(images) > max_files:
                raise ImageInputError("input images exceed the file-count limit")

        directory_after = _FileIdentity.from_stat(os.fstat(directory_descriptor))
        if directory_after != directory_before:
            raise ImageInputError("the input directory changed during preflight")
    finally:
        os.close(directory_descriptor)

    if not images:
        raise ImageInputError("no supported PNG, JPEG, or WebP images were found")
    return images
