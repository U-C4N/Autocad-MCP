# Delivery and round trip

Three different questions, three different pieces of evidence:

| Question | Evidence |
|---|---|
| Is it drawn well? | `drawing_critique(focus=None)` empty; `drafting_quality` (the finalize score) |
| Does it match the source? | `geometry_acceptance`: every requirement measured, `accepted: true` |
| Is the file the one that was checked? | `artifact_integrity`: the sha256 and the requirements re-measured on the saved file; later, the manifest re-hashed |

A high score never answers the second question, and a pass in memory never
answers the third.

## Save and reverify

```text
drawing_finalize(save_path="out/flange.dxf", screenshot_path="out/flange.png",
                 requirements=req)
```

The validator and every critique focus run over one read of every space. With
`requirements`, the requirements are measured on the geometry after the save
and **again on the saved file**, read fresh and re-bound (by handle while
handles are stable, then by feature tag, then by geometry; two candidates are
`unknown`). A requirement that does not pass, stale evidence or a dimension
that disagrees with its geometry raises `failed_acceptance` - whatever the
score.

To check a saved file yourself - after a colleague edited it, or months later:

```text
drawing_verify_requirements(requirements=req, artifact_path="out/flange.dxf")
```

The report carries `rebinding` and a before / after `comparison`. A count or
absence is counted afresh on the file. On the headless engine an artifact must
be DXF.

## Deliver

```text
drawing_deliver(output_dir="out/delivery", formats=["dxf", "pdf", "png"],
                requirements=req)
```

Status `success`, `failed_validation`, `failed_export` or `failed_acceptance`;
failure keeps the artifacts for diagnosis. The open document is never
re-pointed or saved: the DXF is a copy, read back and compared with the
document over every space (zero changed, added or removed entities, the same
block definitions). With `requirements` the bundle gains `acceptance.json` and
`acceptance.md`, and `manifest.json` records every sha256, the document id and
revision, the requirements hash and the build.

## After delivery

```text
drawing_verify_requirements(manifest_path="out/delivery/manifest.json")
```

re-hashes every artifact and lists the ones whose bytes changed (`stale`) or
that are gone (`missing`). A modified artifact is no longer the delivered one:
say so, and measure it again (`artifact_path=`) before anyone relies on it.

## Stale evidence

Evidence records the document id and content revision it was measured at.
Any later write makes it stale; finalize and deliver refuse to accept stale
evidence. Measure again after the last change - never quote an earlier pass.

Reading a delivery result end to end:
[the accepted delivery example](../examples/accepted-delivery-report.md).
