import argparse
import json

from receipt_extractor import file_io as io_mod
from receipt_extractor import gpt

def process_directory(dirpath):
    """Process all receipt files in a directory and extract structured data.

    Args:
        dirpath (str): Path to the directory containing receipt images.

    Returns:
        dict: Mapping of filename to extracted receipt data.
    """
    results = {}
    for name, path in io_mod.list_files(dirpath):
        image_b64 = io_mod.encode_file(path)
        data = gpt.extract_receipt_info(image_b64)
        results[name] = data
    return results

def main():
    """Parse CLI arguments and optionally print extracted data as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("dirpath")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()

    data = process_directory(args.dirpath)
    if args.print:
        print(json.dumps(data, indent=2))

if __name__ == "__main__":
    main()
