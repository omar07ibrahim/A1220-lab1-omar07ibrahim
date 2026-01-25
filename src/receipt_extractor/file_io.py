"""File input/output utilities for receipt processing."""

import base64
import os


def encode_file(path):
    """Encode a file's bytes as a base64 UTF-8 string.

    Args:
        path (str): Path to the file to encode.

    Returns:
        str: Base64-encoded contents of the file.
    """
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def list_files(dirpath):
    """Yield file names and paths for files in a directory.

    Args:
        dirpath (str): Directory to scan for files.

    Yields:
        tuple[str, str]: (filename, full_path) for each file in the directory.
    """
    for name in os.listdir(dirpath):
        path = os.path.join(dirpath, name)
        if os.path.isfile(path):
            yield name, path
