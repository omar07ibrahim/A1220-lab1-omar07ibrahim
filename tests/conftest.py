from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest
from PIL import Image


class ImageFactory(Protocol):
    def __call__(
        self,
        path: Path,
        image_format: str,
        *,
        color: str = "navy",
        size: tuple[int, int] = (3, 2),
    ) -> bytes: ...


@pytest.fixture
def image_factory() -> ImageFactory:
    def create(
        path: Path,
        image_format: str,
        *,
        color: str = "navy",
        size: tuple[int, int] = (3, 2),
    ) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGB", size, color) as image:
            image.save(path, format=image_format, lossless=True)
        return path.read_bytes()

    return create


@pytest.fixture
def receipt_dir(tmp_path: Path, image_factory: ImageFactory) -> Iterator[Path]:
    directory = tmp_path / "receipts"
    directory.mkdir()
    image_factory(directory / "b.PNG", "PNG", color="blue")
    image_factory(directory / "A.jpg", "JPEG", color="red")
    yield directory
