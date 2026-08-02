# Synthetic evaluation contract v1

This contract calibrates an exact evaluator against repository-authored
synthetic truth and one repository-authored deliberately imperfect negative
control. It does not evaluate a live model and does not support an OCR or model
accuracy claim.

## Comparison boundary

Every truth and candidate object must first pass the same strict
`ReceiptFields` boundary used by live extraction and offline replay. Comparison
then uses exact typed equality in this fixed order:

1. `date`
2. `amount`
3. `vendor`
4. `category`

There is no date parsing, currency normalization, fuzzy vendor matching,
Unicode normalization, tolerance, embedding similarity, or locale inference.
Surrounding whitespace and blank-to-null behavior come only from the existing
`ReceiptFields` validation contract.

Each field has exactly one outcome:

- `exact`: both validated values are equal, including `null == null`;
- `omission`: truth is non-null and the control is null;
- `spurious`: truth is null and the control is non-null;
- `substitution`: both values are non-null and unequal.

The pinned evaluator-contract identity covers that boundary, the exact
`ReceiptFields` contract digest, the field and outcome order, and the
category-confusion label order. A separate domain-separated suite identity
covers the evaluator identity and the complete ordered semantic suite. The
suite also binds exact image descriptors through the existing replay batch
digest.

## Balanced calibration fixture

Version 1 uses eight visibly synthetic cases. The negative control is authored
so every field exercises every error branch exactly once while two records stay
fully exact:

| Case | date | amount | vendor | category |
| --- | --- | --- | --- | --- |
| `exact-cafe` | exact | exact | exact | exact |
| `metro-amount-category` | exact | substitution | exact | omission |
| `hotel-date-vendor` | omission | exact | substitution | exact |
| `office-date-amount` | substitution | omission | exact | exact |
| `cinema-vendor-category` | exact | exact | omission | substitution |
| `exact-kiosk` | exact | exact | exact | exact |
| `null-date-vendor` | spurious | exact | spurious | exact |
| `null-amount-category` | exact | spurious | exact | spurious |

The exact expected totals are:

- each field: `5 exact / 1 omission / 1 spurious / 1 substitution`;
- all field slots: `20 exact / 32 total`;
- completely exact records: `2 / 8`.

Those ratios describe agreement with an authored control fixture. They are not
estimates, confidence intervals, population metrics, or model-quality scores.

## Aggregate evaluation receipt

The evaluator emits integer counts rather than rounded floating-point scores.
For each field it records the four outcome counts, then reconciles them against
an all-field total, a five-bin histogram of records with zero through four exact
fields, and a fixed-label category confusion matrix. The eight-case fixture has
histogram `[0, 0, 6, 0, 2]`; its category matrix contains eight observations.

The report binds the suite ID, input batch digest, evaluator contract, truth
origin, and negative-control identity. Its domain-separated `report_id` covers
the complete aggregate report body. It deliberately omits case-level rows and
all free-text, date, and amount values; the fixed category taxonomy remains as
confusion-matrix labels, and nonzero cells disclose aggregate truth/candidate
category pairs. A one-case or sparse suite can therefore reveal individual
category values. Ratios such as `20 / 32` field agreement and `2 / 8`
exact-record agreement remain visibly derivable without duplicating or rounding
their denominators.

## Offline CLI

The separate evaluator entry point never imports a provider and needs no API
key or network access:

```console
receipt-evaluator evaluate SUITE.json --format json
receipt-evaluator evaluate SUITE.json --format text
receipt-evaluator verify SUITE.json REPORT.json
```

JSON is the default evaluation format. Text output keeps exact integer ratios
instead of adding lossy percentages. Verification reloads both bounded files,
recomputes the complete report from the suite, and compares canonical report
bytes. Success emits only a fixed three-field verification object. Validation
failures return status 2 with a data-free message; output failures return status
1 without exposing paths or suite contents.

## Identity and trust boundary

`suite_id` is SHA-256 over a domain separator and canonical JSON containing the
suite kind, schema version, and complete semantic body. It is a deterministic
mismatch guard, not a signature, authorship proof, timestamp, or tamper-proof
record. The suite contains field values and input descriptors; a real suite
would be sensitive even when its later aggregate evaluation receipt omits those
values. The same trust limit applies to `report_id`. Suite and input digests are
stable, linkable fingerprints, not anonymization; aggregate counts can also be
sensitive when evaluated on real data. A standalone internally valid report ID
does not prove that its metrics came from the named suite; verification must
recompute the report from that suite and compare the complete result.

The public demo must remain synthetic, offline, source-bound, and reproducible.
Input names are direct-child basenames: absolute, parent-relative, nested, and
Windows-style paths are rejected. No API key, provider import, network request,
wall-clock measurement, random sample, hostname, PID, or absolute path belongs
in the suite or its evidence.
