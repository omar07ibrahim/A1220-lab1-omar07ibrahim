# Content-addressed replay run provenance

Status: core schema, CLI bundle creation, and offline verification are
implemented; reproducible synthetic evidence is the remaining milestone.

The existing replay manifest answers a pre-execution question:

> Which exact ordered image batch and authored typed outputs may this offline
> replay consume?

It does not record the result that a particular invocation actually
materialized. The next milestone adds one private, content-addressed run bundle
that binds the validated input batch, ordered typed results, receipt-field
contract, and exact replay-manifest bytes. A separate offline verifier will
recompute every binding from the bundle, manifest, and current input directory.

This is a local consistency receipt. It is not a signature, trusted timestamp,
authorship proof, software-supply-chain attestation, or evidence that a remote
model ran.

## Scope and non-goals

Version 1 is deliberately replay-only:

- no provider request, API key, network access, camera, or private benchmark;
- no timestamps, usernames, hostnames, process IDs, absolute paths, or random
  identifiers;
- no claim about model identity, model quality, extraction accuracy, or the
  truth of a receipt;
- no append-only transparency log whose unfixed head could be rewritten along
  with its entries;
- no sidecar whose partial publication could disagree with a result file.

Live-mode provenance is outside v1. The provider does not return a signed
statement that this offline verifier can authenticate, so hashing a local live
response would prove only self-consistency while inviting a stronger model-run
claim.

## Canonical bundle

The planned top-level document is `receipt-extractor-run` schema version 1:

```json
{
  "kind": "receipt-extractor-run",
  "schema_version": 1,
  "run_id": "sha256:…",
  "body": {
    "mode": "replay",
    "contract": {
      "id": "receipt-extractor/receipt-fields",
      "schema_version": 1,
      "digest": "sha256:…"
    },
    "input_batch_digest": "sha256:…",
    "replay_manifest_file_sha256": "sha256:…",
    "items": [
      {
        "input_name": "cafe-lumen.png",
        "output": {
          "date": "2026-07-24",
          "amount": "$12.50",
          "vendor": "Synthetic Market",
          "category": "Other"
        }
      }
    ]
  }
}
```

`items` is an ordered list, not a receipt-name map. This makes input/output
association and order part of the run ID rather than relying on JSON object
insertion order. Input names must be unique, safe direct-child names and must
exactly match the preflighted batch and replay manifest in order. Every output
passes the same strict `ReceiptFields` validation used by replay and live
extraction and must equal the corresponding authored manifest output.

The entire document is strict:

- unknown, missing, duplicated, or coerced fields fail;
- UTF-8 BOM, invalid UTF-8, `NaN`, and infinity fail;
- digests use lowercase `sha256:` plus exactly 64 hexadecimal characters;
- the non-empty ordered item list stays within the existing batch limit;
- canonical JSON is ASCII, sorted by key, compact, and rejects non-finite
  values.

Presentation serialization may be indented. Digests always use the compact
canonical form.

## Domain-separated identities

Each identity hashes a versioned domain prefix, a zero byte, and canonical JSON
bytes:

| Identity | Canonical payload | Purpose |
| --- | --- | --- |
| input batch | existing ordered replay descriptors | Exact dataset mismatch guard |
| contract digest | explicit receipt contract document | Exact validation semantics |
| run ID | top-level kind/version plus the complete body | One identifier for every binding and ordered output |

The prefixes are fixed independently:

```text
auditable-receipt-extractor/replay-batch/v1\0
auditable-receipt-extractor/receipt-contract/v1\0
auditable-receipt-extractor/replay-run/v1\0
```

The run ID hashes canonical
`{"kind":"receipt-extractor-run","schema_version":1,"body":...}` and excludes
only the `run_id` field itself. Changing order, an input descriptor, one result
field, contract semantics, a manifest byte, or any body field must change the
run ID.

These hashes are mismatch guards. Anyone able to edit a bundle can recompute
all of them. They provide no secret-key authenticity and no proof of when or by
whom the file was created.

## Receipt contract identity

Hashing Python source would bind irrelevant formatting and still omit runtime
validation behavior. The contract digest instead covers this exact versioned
document:

```json
{
  "id": "receipt-extractor/receipt-fields",
  "schema_version": 1,
  "object": {
    "required": ["date", "amount", "vendor", "category"],
    "strict_scalar_types": true,
    "unknown_fields": "reject"
  },
  "fields": {
    "date": {
      "types": ["string", "null"],
      "max_length": 64,
      "strip_surrounding_whitespace": true,
      "blank_to_null": true,
      "reject_unicode_general_category_prefixes": ["C"]
    },
    "amount": {
      "types": ["string", "null"],
      "max_length": 64,
      "strip_surrounding_whitespace": true,
      "blank_to_null": true,
      "reject_unicode_general_category_prefixes": ["C"]
    },
    "vendor": {
      "types": ["string", "null"],
      "max_length": 200,
      "strip_surrounding_whitespace": true,
      "blank_to_null": true,
      "reject_unicode_general_category_prefixes": ["C"]
    },
    "category": {
      "types": ["enum", "null"],
      "values": [
        "Meals",
        "Transport",
        "Lodging",
        "Office Supplies",
        "Entertainment",
        "Other"
      ]
    }
  }
}
```

The normalization rules apply only to `date`, `amount`, and `vendor`.
`category` is a strict closed enum: whitespace-padded, blank, unknown, or
control-bearing values are rejected rather than normalized.

The Pydantic dependency is pinned. Golden-vector tests will freeze the exact
contract document and digest, then cross-check each literal rule against
`ReceiptFields`. A semantic contract change requires an explicit schema-version
decision; it must not silently inherit the old identity.

## Creation workflow

The existing replay path remains backward compatible:

```bash
receipt-extractor inputs \
  --replay replay-manifest.json \
  --output replay-result.json
```

The new sink requests the single-file bundle:

```bash
receipt-extractor inputs \
  --replay replay-manifest.json \
  --run-output replay-run.json
```

`--run-output` is mutually exclusive with `--output` and `--stdout`, is valid
only with `--replay`, and uses the existing private exclusive no-clobber output
reservation. The file is mode `0600`; its parent must be current-user-owned and
not group- or world-writable. One committed file avoids a result/sidecar
partial-publication state.

Creation must:

1. preflight every image through the production image boundary;
2. safely read and validate the exact replay manifest;
3. bind its ordered descriptors to the preflighted images;
4. consume every typed authored output exactly once;
5. build ordered run items from that same image/result sequence;
6. compute the contract identity and whole-body run ID;
7. validate the completed bundle again before serialization;
8. durably commit the exclusive private reservation.

OpenAI must not be imported. Any handled failure before durable commit removes
the still-identical reservation or emits the existing cleanup warning.

## Offline verification workflow

Verification is a separate execution mode:

```bash
receipt-extractor inputs \
  --verify-run replay-run.json \
  --against-manifest replay-manifest.json
```

It accepts no remote-upload acknowledgement and no output sink. On success it
prints only an aggregate summary:

```json
{
  "mode": "verify-run",
  "schema_version": 1,
  "verified": true
}
```

The fixed summary omits even the receipt count, along with filenames, extracted
fields, manifest hashes, run ID, paths, and timestamps. Failure text is stable
and redacted.

The verifier must independently:

1. preflight the current input directory;
2. read both JSON files through the shared bounded descriptor-pinned reader;
3. validate duplicate keys, strict encoding, and both schemas;
4. verify the replay manifest's internal input-batch digest;
5. compare its ordered descriptors with the current images;
6. require `body.input_batch_digest`, `manifest.batch.digest`, and the digest
   recomputed from current descriptors to be exactly equal;
7. compare run item names with that exact order;
8. revalidate every `ReceiptFields` output;
9. compare every typed output with the corresponding manifest output;
10. recompute the current contract identity;
11. compare the exact raw replay-manifest SHA-256;
12. recompute and compare the run ID over the whole canonical body.

Every comparison is required. A valid run bundle against the wrong
re-serialized manifest fails even if the manifest is semantically equivalent,
because the run body deliberately binds its exact bytes.

## Shared input boundary

Replay and run verification need the same bounded JSON-file guarantees. The
first implementation commit will extract the existing replay reader into a
small shared module while preserving replay behavior and error redaction:

- `.json` leaf and safely encodable path components;
- descriptor traversal without following symlinks or `..`;
- bounded, non-empty, single-link regular file;
- identity equality before open, after read, and through final name lookup;
- parent identity stability;
- duplicate-key, non-finite, BOM, invalid-UTF-8, recursion, and strict-schema
  rejection.

The refactor must be behavior-preserving for the existing replay manifest
before provenance code depends on it.

## Privacy boundary

A run bundle contains filenames, correlatable content hashes through its input
binding, and extracted receipt fields. For real receipts it is as sensitive as
the original result and replay manifest. It must not be committed, pasted into
issues, written to a shared directory, or emitted to stdout by default.

Even a run ID can correlate identical content across copies, so the verifier
does not print it. The checked-in example will use only visibly synthetic,
repository-authored fixtures.

## Evidence plan

Committed evidence will come only from the existing deterministic synthetic
receipt pair and must be rebuilt by `demo-check`:

1. a real `replay-run.json` produced by the production CLI;
2. a real redacted `run-verification.json` capture;
3. a terminal screenshot of creation and verification;
4. a source/data-backed provenance diagram showing four hash bindings—the
   existing input identity, contract identity, whole-body run ID, and raw
   manifest-file SHA-256—and every verifier edge;
5. an updated architecture diagram and CLI-help capture;
6. a seventh GIF frame for verification;
7. an exact manifest binding source, fixtures, commands, and outputs.

Existing receipt montage, replay capture, and failure gallery should remain
byte-identical unless their actual command contract changes. The generator
must fail on any unexpected artifact rather than accepting extra files.

## Required verification

- pinned golden vectors for both new digest domains;
- sensitivity to every body, item, output, contract, and order field;
- canonical stability across JSON whitespace and key order;
- exact typed output/name association;
- duplicate-key, unknown-field, coercion, BOM, invalid UTF-8, non-finite,
  oversized, and malformed-digest rejection;
- symlink, hardlink, FIFO, traversal, concurrent-replacement, and parent-change
  cases for both artifacts;
- wrong input bytes, dimensions, order, manifest bytes, contract, result, and
  run ID;
- private `0600` no-clobber output and cleanup behavior;
- two independent creations produce byte-identical bundles;
- provider poison module proves creation and verification do not import OpenAI;
- current replay output remains byte-identical;
- generated evidence rebuilds byte-for-byte.
