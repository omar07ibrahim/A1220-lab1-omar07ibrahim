# Receipt Extractor (Lab 1)

This project is a small command-line app that processes a directory of receipt
images. For each receipt, it calls the OpenAI API to extract:

- date
- amount
- vendor
- category (one of the predefined categories)

The output is a single JSON object mapping each filename to its extracted data.

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your OpenAI API key (a helper command is in `COMMAND_API_OPENAI.txt`):

```bash
export OPENAI_API_KEY=your_key_here
```

## Run

Run directly with Python:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts --print
```

Or use the Makefile target:

```bash
make run OPENAI_API_KEY=$OPENAI_API_KEY
```

You can override the input directory:

```bash
make run DIR=path/to/receipts OPENAI_API_KEY=$OPENAI_API_KEY
```

## Documentation

Generate docs with pdoc:

```bash
pdoc src/receipt_extractor -o docs
```

The `docs/` directory is ignored by Git.

## Project Layout

```
src/receipt_extractor/
  file_io.py       # File loading and encoding helpers
  gpt.py           # OpenAI API interaction
  main.py          # CLI entry point
```

## License

MIT License. See `LICENSE`.
