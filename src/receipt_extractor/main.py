"""Command-line entry point for bounded receipt extraction."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from receipt_extractor import file_io, postprocess

Extractor = Callable[[file_io.ImagePayload], dict[str, Any]]


class ProviderExecutionError(RuntimeError):
    """Hide provider-specific failures at the CLI logging boundary."""


class OutputError(ValueError):
    """Raised when a result cannot be published without clobbering data."""


@dataclass(slots=True)
class _ReservedOutput:
    directory_descriptor: int
    file_descriptor: int
    final_name: str
    device: int
    inode: int
    _committed: bool = False
    _closed: bool = False

    def commit(self, payload: str) -> None:
        """Write and durably commit the exclusively reserved output file."""
        if self._closed or self._committed:
            raise OutputError("the output reservation is no longer writable")
        encoded = f"{payload}\n".encode()
        view = memoryview(encoded)
        while view:
            written = os.write(self.file_descriptor, view)
            if written < 1:
                raise OutputError("could not write the reserved output file")
            view = view[written:]
        os.fchmod(self.file_descriptor, 0o600)
        os.fsync(self.file_descriptor)
        details = os.fstat(self.file_descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_dev != self.device
            or details.st_ino != self.inode
            or details.st_size != len(encoded)
        ):
            raise OutputError("the reserved output changed while it was being written")
        try:
            named = os.stat(
                self.final_name,
                dir_fd=self.directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise OutputError("could not verify the reserved output path") from error
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or named.st_dev != self.device
            or named.st_ino != self.inode
        ):
            raise OutputError("the reserved output path changed before commit")
        self._committed = True
        try:
            os.fsync(self.directory_descriptor)
        except OSError as error:
            raise OutputError(
                "output data was written but directory durability is uncertain; "
                "inspect the destination"
            ) from error

    def close(self) -> bool:
        """Close descriptors and remove an uncommitted reservation when safe."""
        if self._closed:
            return True
        self._closed = True
        cleanup_succeeded = True
        try:
            if not self._committed:
                try:
                    named = os.stat(
                        self.final_name,
                        dir_fd=self.directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_succeeded = False
                else:
                    if named.st_dev == self.device and named.st_ino == self.inode:
                        try:
                            os.unlink(
                                self.final_name,
                                dir_fd=self.directory_descriptor,
                            )
                            os.fsync(self.directory_descriptor)
                        except OSError:
                            cleanup_succeeded = False
                    else:
                        cleanup_succeeded = False
        finally:
            try:
                os.close(self.file_descriptor)
            except OSError:
                cleanup_succeeded = False
            finally:
                try:
                    os.close(self.directory_descriptor)
                except OSError:
                    cleanup_succeeded = False
        return cleanup_succeeded


def process_images(
    images: Sequence[file_io.ImagePayload],
    extractor: Extractor,
) -> dict[str, dict[str, Any]]:
    """Extract and normalize an already validated image batch."""
    results: dict[str, dict[str, Any]] = {}
    for image in images:
        data = extractor(image)
        results[image.name] = postprocess.normalize_amount(data)
    return results


def process_directory(
    dirpath: str | os.PathLike[str],
    *,
    extractor: Extractor | None = None,
    max_files: int = file_io.DEFAULT_MAX_FILES,
    max_file_bytes: int = file_io.DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = file_io.DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, dict[str, Any]]:
    """Validate all images before making a bounded sequence of API calls."""
    images = file_io.load_images(
        dirpath,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    if extractor is None:
        from receipt_extractor import gpt

        extractor = gpt.extract_receipt_info
    return process_images(images, extractor)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a bounded directory of receipt images and extract "
            "structured fields."
        )
    )
    parser.add_argument("dirpath", help="directory containing direct-child images")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and emit local audit metadata without importing OpenAI",
    )
    parser.add_argument(
        "--acknowledge-remote-upload",
        action="store_true",
        help="confirm that image bytes and embedded metadata may leave this machine",
    )
    parser.add_argument(
        "--max-files",
        type=_positive_int,
        default=file_io.DEFAULT_MAX_FILES,
        help=f"maximum images per batch (default: {file_io.DEFAULT_MAX_FILES})",
    )
    parser.add_argument(
        "--max-file-mib",
        type=_positive_int,
        default=file_io.DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
        help="maximum encoded bytes per image in MiB (default: 10)",
    )
    parser.add_argument(
        "--max-total-mib",
        type=_positive_int,
        default=file_io.DEFAULT_MAX_TOTAL_BYTES // (1024 * 1024),
        help="maximum encoded bytes across the batch in MiB (default: 50)",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--output",
        type=Path,
        help="reserve a private JSON file; existing paths are never replaced",
    )
    destination.add_argument(
        "--stdout",
        "--print",
        dest="stdout",
        action="store_true",
        help="explicitly emit extracted receipt fields to stdout",
    )
    return parser


def _reserve_private_output(path: Path) -> _ReservedOutput:
    try:
        os.fsencode(path)
    except UnicodeError as error:
        raise OutputError("the output path is not safely encodable") from error
    if any(
        unicodedata.category(character).startswith("C")
        for component in path.parts
        for character in component
    ):
        raise OutputError(
            "the output path must not contain control or format characters"
        )

    final_name = path.name
    if (
        not final_name
        or final_name in {".", ".."}
        or Path(final_name).suffix.lower() != ".json"
    ):
        raise OutputError("the output path must name a new .json file")

    parent = path.parent if os.fspath(path.parent) else Path(".")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_descriptor = os.open(os.fspath(parent), directory_flags)
    except (OSError, UnicodeError) as error:
        raise OutputError(
            "the output parent must be an existing, non-symlink directory"
        ) from error

    file_descriptor = -1
    created = False
    succeeded = False
    cleanup_succeeded = True
    failure: OutputError | None = None
    reservation: _ReservedOutput | None = None
    try:
        parent_details = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(parent_details.st_mode)
            or parent_details.st_uid != os.geteuid()
            or parent_details.st_mode & 0o022
        ):
            raise OutputError(
                "the output parent must be owned by the current user and not "
                "group- or world-writable"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        file_descriptor = os.open(
            final_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        created = True
        details = os.fstat(file_descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OutputError("could not reserve a single-link regular output file")
        os.fchmod(file_descriptor, 0o600)
        reservation = _ReservedOutput(
            directory_descriptor=directory_descriptor,
            file_descriptor=file_descriptor,
            final_name=final_name,
            device=details.st_dev,
            inode=details.st_ino,
        )
        succeeded = True
    except OutputError as error:
        failure = error
    except FileExistsError as error:
        failure = OutputError("the output path already exists; refusing to replace it")
        failure.__cause__ = error
    except (OSError, UnicodeError) as error:
        failure = OutputError("could not safely reserve the output file")
        failure.__cause__ = error
    finally:
        if created and not succeeded:
            try:
                os.close(file_descriptor)
            except OSError:
                cleanup_succeeded = False
            try:
                os.unlink(final_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError:
                cleanup_succeeded = False
        if not succeeded:
            try:
                os.close(directory_descriptor)
            except OSError:
                cleanup_succeeded = False

    if not succeeded:
        if not cleanup_succeeded:
            raise OutputError(
                "output reservation failed and cleanup could not be confirmed; "
                "inspect the destination"
            ) from failure
        if failure is not None:
            raise failure
        raise OutputError("could not reserve the output file")
    if reservation is None:
        raise OutputError("could not reserve the output file")
    return reservation


def _serialize(result: object) -> str:
    try:
        return json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("result is not finite, JSON-compatible data") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI without importing or initializing the provider for help/dry-run."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run:
        if not args.acknowledge_remote_upload:
            parser.error("--acknowledge-remote-upload is required for live extraction")
        if args.output is None and not args.stdout:
            parser.error("live extraction requires either --output or --stdout")
        if not os.environ.get("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY must be set for live extraction")

    max_file_bytes = args.max_file_mib * 1024 * 1024
    max_total_bytes = args.max_total_mib * 1024 * 1024
    try:
        images = file_io.load_images(
            args.dirpath,
            max_files=args.max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"input validation failed: {error}\n")

    output: _ReservedOutput | None = None
    try:
        if args.output is not None:
            output = _reserve_private_output(args.output)

        if args.dry_run:
            result: dict[str, Any] = {
                "schema_version": 1,
                "mode": "dry-run",
                "count": len(images),
                "images": [image.audit_metadata() for image in images],
            }
        else:
            try:
                result = process_images(images, _openai_extract)
            except (ProviderExecutionError, TypeError, ValueError):
                parser.exit(
                    1,
                    "extraction failed; provider details are suppressed to avoid "
                    "leaking receipt data\n",
                )

        try:
            serialized = _serialize(result)
        except ValueError:
            parser.exit(1, "result serialization failed; details are suppressed\n")

        if output is not None:
            output.commit(serialized)
        else:
            sys.stdout.write(f"{serialized}\n")
    except (OSError, OutputError) as error:
        parser.exit(1, f"output failed: {error}\n")
    finally:
        if output is not None and not output.close():
            sys.stderr.write(
                "warning: private output cleanup could not be confirmed; "
                "inspect the destination directory\n"
            )
    return 0


def _openai_extract(image: file_io.ImagePayload) -> dict[str, Any]:
    try:
        from receipt_extractor import gpt

        return gpt.extract_receipt_info(image)
    except Exception as error:
        raise ProviderExecutionError from error


if __name__ == "__main__":
    raise SystemExit(main())
