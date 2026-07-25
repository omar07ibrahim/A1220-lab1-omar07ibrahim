# Auditable Receipt Extractor

A privacy-explicit multimodal pipeline that validates an entire receipt-image
batch before sending the first byte, parses model output into a strict schema,
and can reproduce an exact recorded batch without OpenAI, a key, or a network
request.

This repository began as a small A1220 lab. It is being rehabilitated through
reviewable commits into a document-intelligence project with bounded inputs,
typed outputs, deterministic replay, and evidence that does not rely on private
receipts.

![Five-step synthetic receipt demo: generate, inspect, preflight, replay, and verify](docs/assets/demo.gif)

The animation above is rebuilt from the current source, generated fixtures,
captured CLI stdout, and coverage.py data. It is not a recording of a model
call.

## What works today

- PNG, JPEG, and WebP are checked by extension, signature, decoded format, and
  exact container boundary.
- Every accepted image is fully decoded under a 25-megapixel ceiling.
- Directory and file descriptors are pinned while identities are checked
  before and after bounded reads.
- The complete batch is preflighted before the first provider request.
- OpenAI Responses parsing targets a strict four-field Pydantic contract.
- Offline replay binds a versioned manifest to the exact ordered image batch.
- Result files are private, exclusive, no-clobber reservations; stdout is
  opt-in.
- Provider failures are redacted at the CLI boundary.

The project does **not** claim live-model accuracy yet. Public tests and
examples use synthetic images, and no live API call is needed to verify the
engineering claims.

## Verified synthetic demo

The repository ships one real PNG and one lossless WebP fixture. Both are
generated deterministically, visibly marked synthetic, decoded by the
production preflight, and bound to the checked-in replay manifest by name,
MIME type, byte size, SHA-256, width, and height.

![Two generated synthetic receipt inputs](docs/assets/demo-receipts.png)

Their outputs are authored fixture truth for exercising the pipeline—not
evidence that a model read the images. The exact source artifacts are
[the generated inputs](demo/inputs),
[the strict manifest](demo/replay-manifest.json), and
[the captured replay result](demo/evidence/replay-result.json).

### Architecture

![Architecture from receipt batch through preflight, execution mode, typed boundary, and sink](docs/assets/architecture.svg)

Replay and live extraction deliberately converge on the same `ReceiptFields`
validation boundary. Dry-run stops after preflight and emits only audit
metadata. Replay substitutes a strictly bound local provider; it does not
weaken image preflight or output safety.

## Setup

The supported runtime is Python 3.12 on Linux.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

![Actual source CLI help captured by the demo generator](docs/assets/cli-help.png)

Install the separately pinned development tools and run the complete offline
gate:

```bash
pip install -r requirements-dev.txt
make check
```

`make audit` is intentionally separate because dependency-advisory lookup is a
networked operation. The regular gate uses generated images and fake providers;
it makes no model request. The current gate is 121 tests with 91% combined
statement and branch coverage.

![Coverage generated from the current coverage.py JSON](docs/assets/coverage.svg)

The bounded source data is checked in as
[`demo/evidence/coverage-summary.json`](demo/evidence/coverage-summary.json).

## Inspect a batch locally

Dry-run validates every direct-child image and prints only audit metadata:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts --dry-run
```

```json
{
  "count": 2,
  "mode": "dry-run",
  "schema_version": 1,
  "images": [
    {
      "height": 900,
      "media_type": "image/png",
      "name": "receipt-001.png",
      "sha256": "…",
      "size_bytes": 48122,
      "width": 600
    }
  ]
}
```

Names and digests are still sensitive metadata: filenames may contain personal
information, and hashes can be correlatable. Dry-run output is inspection data,
not automatically safe publication evidence.

This capture is rendered from the actual command output stored in
[`demo/evidence/dry-run.json`](demo/evidence/dry-run.json):

![Actual dry-run CLI output for the two synthetic fixtures](docs/assets/cli-dry-run.png)

## Reproduce a recorded batch offline

A replay manifest contains an ordered input descriptor and one typed output for
every receipt. Its batch digest is a domain-separated SHA-256 over the canonical
ordered descriptors.

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts \
  --replay replay-manifest.json \
  --output replay-result.json
```

Replay performs these steps before reserving the result:

1. preflight every image through the normal image boundary;
2. read the manifest through pinned descriptors without following symlinks;
3. reject hard links, oversized files, duplicate JSON keys, non-finite values,
   invalid UTF-8, unknown fields, and scalar coercion;
4. verify the manifest's internal digest;
5. compare every ordered name, MIME type, byte size, SHA-256, width, and height
   against the actual validated batch.

Only then are recorded outputs consumed exactly once. `--replay` is mutually
exclusive with dry-run and rejects the remote-upload acknowledgement. It does
not require `OPENAI_API_KEY` and does not import the OpenAI package.

The digest is a **dataset mismatch guard**, not a signature, timestamp, or
proof of model provenance. Anyone able to edit a manifest can recompute it.
Replay manifests also contain extracted receipt fields and must be protected
like the original results.

The checked-in manifest produces this exact current output without a key or
OpenAI import:

![Actual exact-batch replay CLI output](docs/assets/cli-replay.png)

## Run live extraction

Set the key only in the process environment. Never place it in a repository
file or pass its value as a Make argument.

```bash
export OPENAI_API_KEY=your_key_here
PYTHONPATH=src python -m receipt_extractor.main receipts \
  --acknowledge-remote-upload \
  --output result.json
```

Live mode requires all three deliberate choices:

- `OPENAI_API_KEY` is present;
- `--acknowledge-remote-upload` confirms that original image bytes and embedded
  metadata may leave the machine;
- either `--output` or `--stdout` selects where sensitive results go.

The adapter uses `gpt-4.1-mini`, image detail `high`, and typed
`client.responses.parse(...)` output. The provider-facing contract requires all
four fields while allowing an unknown field value to be `null`:

```json
{
  "date": "2026-07-24",
  "amount": "$12.50",
  "vendor": "Synthetic Market",
  "category": "Other"
}
```

`amount` remains text so currency marks and printed formatting are not silently
lost. Category is closed to Meals, Transport, Lodging, Office Supplies,
Entertainment, or Other. Extra keys, missing keys, numeric coercion, unknown
categories, and hidden control characters fail the boundary.

OpenAI documents image input for the
[Responses API](https://developers.openai.com/api/docs/guides/images-vision)
and typed
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The request sets `store=False`; this should not be interpreted as a universal
zero-retention guarantee. Review the current provider terms and your
organization's data controls before processing real receipts.

## Output safety

`--output` reserves a brand-new `0600` `.json` file with `O_EXCL` before any
live provider call. Existing regular files, links, FIFOs, and receipt inputs are
never replaced. The parent must be owned by the current user, not group- or
world-writable, and not a symlink.

Handled failures remove an uncommitted reservation when its identity can still
be proven. If cleanup or directory durability cannot be confirmed, the CLI
warns and asks the operator to inspect the destination. A host crash can also
leave an empty or partial private file. Processes under the same OS account
remain inside this local trust boundary.

`--stdout` is explicit because vendor names and amounts can otherwise enter
shell history, CI logs, or terminal capture:

```bash
PYTHONPATH=src python -m receipt_extractor.main receipts \
  --acknowledge-remote-upload \
  --stdout
```

## Bounded threat model

| Boundary | Enforced | Not claimed |
| --- | --- | --- |
| Image discovery | direct children, entry/file/batch limits, deterministic order | hostile kernel or compromised same-user process |
| Image parsing | exact PNG/JPEG/WebP bounds, full decode, frame and pixel limits | malware scanning or metadata removal |
| Provider | explicit upload acknowledgement, bounded retries/timeout, typed response | model accuracy or zero retention |
| Replay | exact ordered batch, strict manifest, pinned reads | signature, authenticity, or independent attestation |
| Output | private no-clobber file, held descriptor, cleanup checks | atomic recovery from power or host failure |

Caught provider exception text is not printed. This narrow guarantee does not
cover SDK or HTTP debug logging, so leave such logging disabled for sensitive
data.

## Rebuild every visual

Visual evidence is generated by
[`scripts/capture_demo.py`](scripts/capture_demo.py), not edited by hand. The
exact help transcript is also available as
[`demo/evidence/help.txt`](demo/evidence/help.txt). The generator:

1. creates both visibly synthetic receipts;
2. loads them through `file_io.load_images`;
3. builds the canonical exact-batch manifest;
4. executes the real help, dry-run, and replay commands with no API key;
5. captures the exact text outputs;
6. reads coverage.py JSON from the full test run;
7. renders the terminal captures, diagrams, chart, and GIF.

Regenerate the tracked artifacts:

```bash
make demo
```

Prove they are current without touching the tracked copies:

```bash
make demo-check
```

`demo-check` rebuilds everything under `.venv/demo-check` and recursively
compares the result byte-for-byte with `demo/` and `docs/assets/`. If a code,
fixture, command, manifest, test-count, coverage, or rendering change affects
the evidence, the check fails until the visual refresh is intentional.

## Project layout

```text
src/receipt_extractor/
  file_io.py       # pinned-FD discovery, bounded reads, decode, and data URLs
  gpt.py           # typed OpenAI Responses adapter
  main.py          # CLI, provider boundary, result validation, private output
  replay.py        # exact-batch manifest loader and offline provider
  schema.py        # strict receipt and category contracts
scripts/
  capture_demo.py  # fixtures, real CLI captures, diagrams, chart, and GIF
demo/
  inputs/          # visibly synthetic PNG and lossless WebP receipts
  evidence/        # exact captured stdout plus coverage summary
  replay-manifest.json
docs/assets/       # generated README visuals; checked by make demo-check
tests/
  test_file_io.py          # real formats, limits, links, and TOCTOU regressions
  test_cli.py              # privacy modes, replay integration, output failures
  test_demo_evidence.py    # manifest, captures, image/GIF/SVG integrity
  test_gpt.py              # fake typed Responses calls; never a live request
  test_replay.py           # manifest grammar, path attacks, exact-batch binding
  test_schema.py           # strict field and category adversarial cases
```

## License and provenance

MIT License. See [LICENSE](LICENSE). The original lab history remains visible;
the production-hardening and evidence work is added as later commits rather
than presented as part of the initial submission.
