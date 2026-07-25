# Receipt Extractor

A small multimodal command-line pipeline for extracting a date, amount, vendor,
and expense category from receipt images. The original Lab 1 implementation is
being rehabilitated incrementally into an auditable document-intelligence
project; current claims remain deliberately bounded.

## Current offline-safe boundary

Before making any paid request, the CLI preflights the complete direct-child
batch:

- PNG, JPEG, and WebP only, with extension, signature, decoded format, and
  exact container boundary required to agree;
- a full bounded Pillow decode, single-frame enforcement, and a 25-megapixel
  decompression-bomb ceiling;
- deterministic filename order;
- a pinned directory descriptor plus per-file identity checks before and after
  every bounded read;
- no final-component symlinks, hard links, empty files, Unicode
  control/format names, or nested traversal;
- at most 1,000 direct directory entries inspected;
- at most 20 images, 10 MiB per image, and 50 MiB total by default;
- the correct MIME type in every provider data URL.

Inspect the exact upload candidates without importing OpenAI or needing a key:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts --dry-run
```

The output contains names, dimensions, sizes, media types, and SHA-256
digests—not image bytes or local absolute paths. Treat that audit output as
receipt metadata: filenames can contain personal information and digests can
be correlatable. It is local inspection data, not safe-to-publish evidence.

## Setup

The current hardened I/O slice targets Python 3.12 on Linux. Create and
activate a virtual environment, then install the exact direct runtime
dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For development, install the separately pinned test tools and run the complete
offline gate:

```bash
pip install -r requirements-dev.txt
make check
```

The current gate is 54 synthetic/offline tests with 93% combined statement and
branch coverage. It generates and fully decodes real tiny PNG, JPEG, and WebP
fixtures; exercises TOCTOU swaps, links, FIFOs, corrupt containers,
decompression limits, output cleanup faults, provider redaction, and
non-finite values; and makes no model or network request.

`make audit` is a separate networked dependency-advisory check so the offline
gate remains deterministic.

Set the key only in the process environment. Do not pass its value as a Make
argument or place it in a repository file:

```bash
export OPENAI_API_KEY=your_key_here
```

## Live extraction

Live mode requires an explicit acknowledgement because receipt bytes and
embedded metadata are sent to the configured external API:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts \
  --acknowledge-remote-upload \
  --output result.json
```

`--output` reserves a brand-new private `0600` `.json` file with an exclusive
no-clobber open before any provider call, then writes only through that held
file descriptor. Existing paths—including regular files, links, FIFOs, and
receipt inputs—are never opened for writing or replaced. The output parent
must be owned by the current user, must not be group- or world-writable, and
must itself be a real directory rather than a symlink. Handled failures attempt
to remove the reservation; when cleanup cannot be confirmed, the CLI warns and
the private file must be inspected. A process or host crash can likewise leave
an empty or partial private file that must be inspected before reuse. Processes
running as the same OS account are inside this local trust boundary.

Live results are never printed implicitly. Printing requires a separate,
explicit acknowledgement because vendor names and amounts can enter shell or
CI logs:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts \
  --acknowledge-remote-upload \
  --stdout
```

`make run` inherits `OPENAI_API_KEY` without interpolating or printing it and
uses the no-clobber `result.json` destination. Remove or rename that file
deliberately before another run. Custom directories should use the Python CLI
directly.

## Privacy and cost boundary

- Receipt images can contain names, card fragments, addresses, tax identifiers,
  location data, and EXIF metadata.
- Live mode transmits the original validated bytes to OpenAI. Use only data you
  are authorized to process and review the applicable retention settings.
  Validation does not remove EXIF or other embedded metadata.
- The CLI validates the full batch before the first request and bounds the
  request count and bytes, but each accepted image can incur model cost.
- Caught provider exception text is not printed by the CLI. This narrow
  guarantee does not cover SDK or HTTP debug logging; leave such logging
  disabled when handling receipts.
- Public tests, examples, and visuals in this repository use synthetic data
  only. They are not evidence of live-model accuracy.

## Current model boundary

The provider remains pinned in source to `gpt-4.1-mini`. OpenAI documents that
this model accepts [image input](https://developers.openai.com/api/docs/guides/images-vision)
and supports the Responses API and
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The current adapter still uses Chat Completions plus prompt-only JSON parsing;
that is not a strict response contract. The next rehabilitation slice moves
the adapter to typed Responses parsing and adds an offline replay provider.

## Project layout

```text
src/receipt_extractor/
  file_io.py       # pinned-FD discovery, bounded decode, and data URLs
  gpt.py           # OpenAI adapter
  main.py          # CLI, preflight, output, and provider boundary
  postprocess.py   # amount normalization (scheduled for typed replacement)
tests/
  test_file_io.py  # real image formats, limits, links, and TOCTOU regressions
  test_cli.py      # privacy, no-clobber output, redaction, and failure cleanup
```

## License and provenance

MIT License. See [LICENSE](LICENSE). This repository began as Omar Ibrahim's
A1220 Lab 1 submission; the production-hardening and evidence work is tracked
as later, reviewable commits rather than presented as part of the original lab.
