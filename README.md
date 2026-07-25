# Auditable Receipt Extractor

A privacy-explicit multimodal pipeline that validates an entire receipt-image
batch before sending the first byte, parses model output into a strict schema,
and can reproduce an exact recorded batch without OpenAI, a key, or a network
request.

This repository began as a small A1220 lab. It is being rehabilitated through
reviewable commits into a document-intelligence project with bounded inputs,
typed outputs, deterministic replay, and evidence that does not rely on private
receipts.

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

## Setup

The supported runtime is Python 3.12 on Linux.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install the separately pinned development tools and run the complete offline
gate:

```bash
pip install -r requirements-dev.txt
make check
```

`make audit` is intentionally separate because dependency-advisory lookup is a
networked operation. The regular gate uses generated images and fake providers;
it makes no model request. The current gate is 113 tests with 91% combined
statement and branch coverage.

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

## Project layout

```text
src/receipt_extractor/
  file_io.py       # pinned-FD discovery, bounded reads, decode, and data URLs
  gpt.py           # typed OpenAI Responses adapter
  main.py          # CLI, provider boundary, result validation, private output
  replay.py        # exact-batch manifest loader and offline provider
  schema.py        # strict receipt and category contracts
tests/
  test_file_io.py          # real formats, limits, links, and TOCTOU regressions
  test_cli.py              # privacy modes, replay integration, output failures
  test_gpt.py              # fake typed Responses calls; never a live request
  test_replay.py           # manifest grammar, path attacks, exact-batch binding
  test_schema.py           # strict field and category adversarial cases
```

## License and provenance

MIT License. See [LICENSE](LICENSE). The original lab history remains visible;
the production-hardening and evidence work is added as later commits rather
than presented as part of the initial submission.
