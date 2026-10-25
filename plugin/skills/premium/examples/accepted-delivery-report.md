# Example: reading an accepted delivery

Continues [the flange](dimensioned-flange.md): five requirements pass on the
open drawing and the critique is empty.

## 1. Deliver with the requirements

```json tool=drawing_deliver
{"output_dir": "out/flange-delivery", "formats": ["dxf", "pdf", "png"],
 "requirements": {"units": "mm", "items": [
   {"id": "D1", "quantity": "diameter", "target": {"nominal": 40, "unit": "mm"},
    "features": [{"kind": "feature_tag", "part": "3E", "feature": "bore", "view": "front", "type": "circle"}],
    "provenance": "explicit"}]}}
```

(In practice: the whole set from the flange example.) The result, read field
by field:

| Field | Value | What it proves |
|---|---|---|
| `status` | `success` | validator, parity and acceptance all passed |
| `drafting_quality` | score 100.0, grade A, 0 errors | drafting checks only - not the dimensions |
| `parity` | 35 entities old / new, 0 changed / added / removed, blocks compared | the delivered DXF holds what the document held, every space |
| `artifact_integrity.dxf_export` | `ezdxf.write`, AC1024, `repointed: false` | the open document was not renamed to the copy |
| `artifact_integrity.reverified` | the DXF's sha256, `read: ezdxf.readfile` | the requirements were re-measured on the file itself |
| `geometry_acceptance` | `status: pass`, `accepted: true`, 5 / 5 pass | the source requirements hold on the delivered file |
| `coverage` | 5 measured of 5 required, ratio 1.0 | nothing was skipped as unknown |
| `blocking` | `[]` | no reason to refuse |

The bundle: `drawing.dxf`, `drawing.pdf`, `preview.png`, `acceptance.json`,
`acceptance.md`, `manifest.json` with every sha256.

If `status` were `failed_acceptance`, the score would not matter: read
`blocking` and the failing rows, fix the geometry, deliver again.

## 2. Later: is this still the delivered bundle?

```json tool=drawing_verify_requirements
{"manifest_path": "out/flange-delivery/manifest.json"}
```

Untouched: `ok: true`, `stale: []`, `missing: []`.

After someone appended a line to `drawing.dxf`:

```text
ok: false
stale: [{filename: drawing.dxf, recorded_sha256: 9992...a1af, sha256: 3fb0...b600}]
status: success      <- the status the manifest recorded at delivery, not now
```

Report it as "the DXF is no longer the delivered file". Before anyone uses it,
measure the requirements on it again:

```json tool=drawing_verify_requirements
{"requirements": {"units": "mm", "items": [
   {"id": "D1", "quantity": "diameter", "target": {"nominal": 40, "unit": "mm"},
    "features": [{"kind": "feature_tag", "part": "3E", "feature": "bore", "view": "front", "type": "circle"}],
    "provenance": "explicit"}]},
 "artifact_path": "out/flange-delivery/drawing.dxf"}
```

and deliver a new bundle if it still passes.
