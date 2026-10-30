# AgentCAD v8 runtime submission

Contributor submission, 2026-10-30. AgentCAD's recorded run passes **31/31 on
v8 (100.0)** and **10/10 on the common v2 subset**. This ties the published
AutoCAD MCP Pro runtime score. It does not exceed the 100-point ceiling and
does not assign AgentCAD a score in `source_review.json`.

## Versions and reproduction

- Product: [lizaixi01/AgentCAD, `39fa4b5`](https://github.com/lizaixi01/AgentCAD/tree/39fa4b55899233ab7ac5cbb52714a4767ca164c6).
- Original evaluator: this repository at
  [`58a05dd`](https://github.com/U-C4N/Autocad-MCP/tree/58a05ddfc34a9832c626e82cd88ecdce8e16a62a),
  matrix v8 / evaluator v8.1.
- Interface: official MCP SDK over stdio, isolated product Python, headless
  ezdxf. No live AutoCAD, model/API key, or enabled script tool is needed.

From this repository, using Python 3.11 or later:

```bash
uv sync --locked --extra pdf --extra office
uv pip install --python .venv pypdf==6.21.4
uv run --no-sync python -m benchmarks.run_competitors --server agentcad --backend ezdxf --matrix v8 --timeout 180 --artifact-dir out/agentcad-v8 --publish out/agentcad-v8.json --json
```

The first run uses the existing competitor environment manager to clone the
public product commit under `benchmarks/.competitors/agentcad/` and install it
editable in its own venv. Its measured ezdxf, MCP and pypdf versions are pinned
in the adapter. The evaluator's locked environment remains separate.
`--no-fetch` uses an already prepared environment; if it is missing, the lane
reports `environment_blocked`, without a score. Choose a new artifact directory
for every run. Existing evidence is refused rather than overwritten.

The registration shim imports only the two pinned **evaluator playbooks**
(`examples/autocad_benchmark/run.py` and `cancel_worker.py`), not the product
implementation. It checks the checkout, compares playbook contents with git
blobs before loading, and reports their SHA256 hashes. This avoids maintaining
a second copy of the already measured 31 task playbooks. Reviewable source:
[run.py](https://github.com/lizaixi01/AgentCAD/blob/39fa4b55899233ab7ac5cbb52714a4767ca164c6/examples/autocad_benchmark/run.py),
[cancel_worker.py](https://github.com/lizaixi01/AgentCAD/blob/39fa4b55899233ab7ac5cbb52714a4767ca164c6/examples/autocad_benchmark/cancel_worker.py).
The standard runner remains responsible for task selection, timeout, artifact
hashing and scoring; no reference adapter, task definition, weight or scoring
function is changed.

The standard runner's `git_sha` / `manifest.server_sha` identify this evaluator
checkout. `adapter_metadata.product_sha` and `pinned_sha` identify AgentCAD;
`source_sha256` records the product source bytes. These are different commits.

## Checks and boundaries

The playbooks translate the same task contracts to AgentCAD's own API and
reopen saved DXFs in the evaluator. Partial gates score zero. Deliverables are
copied to a task-specific `retained/` directory before later tasks can reuse
their filenames. `events.jsonl`, `tools.json`, `progress.json`, server stderr
and saved drawings make each verdict inspectable.

- Architecture and mechanical sheets must additionally pass the unchanged
  upstream `run_critique(focus=None)`, `DrawingValidator` and `combine` gate:
  zero critique issues, score at least 90. The original run scored 100 and 92;
  the mechanical warning about `ISO 4014 M12x60` was retained, not filtered.
  File hashes before/after prove that this oracle does not repair the drawing.
- Some upstream domain checks depend on Pro-specific XDATA. They cannot by
  themselves certify AgentCAD's different records. The adapter also reads
  saved rooms, openings, parts, BOMs and balloons through the product API and
  checks independent expected values. These translated domain gates require
  review; a zero from the generic oracle alone is not evidence of equivalence.
- Cancellation is the one internal fixture: a separate product process loads
  the real server worker, holds operation A, cancels its waiter, and proves
  B waits until A ends. A fresh MCP process then reads the saved output. This
  is not described as a pure black-box MCP cancellation test.
- CAD creation/readback uses the product; upstream fixture builders only
  supply synthetic inputs. There are no Autodesk sample drawings in the evidence.
- Runtime means deterministic capability checks, not an autonomous-agent or
  same-model success rate. Timings cannot rank these implementations: AgentCAD
  pays stdio, process startup and disk transactions; Pro mostly runs in process.

AgentCAD's discovery catalog measured 220 tokens versus 503 in the published
Pro v8 report, both using `ratio/v1 (estimate)`, three advertised tools and the
2,000-token ceiling. That is a smaller advertised schema, not a billing-token
measurement or a score tiebreaker. The full catalogs differ in scope.

## Original 31/31 evidence

The immutable AgentCAD evidence commit is
[`a3b336a`](https://github.com/lizaixi01/AgentCAD/tree/a3b336a4535bc3cc2a2435f4ebdc76b90f70153c/examples/autocad_benchmark/results/2026-10-30-v8/integrated):

- [Full report](https://github.com/lizaixi01/AgentCAD/blob/a3b336a4535bc3cc2a2435f4ebdc76b90f70153c/examples/autocad_benchmark/results/2026-10-30-v8/integrated/agentcad.json).
- [Evidence ZIP](https://github.com/lizaixi01/AgentCAD/blob/a3b336a4535bc3cc2a2435f4ebdc76b90f70153c/examples/autocad_benchmark/results/2026-10-30-v8/integrated/evidence.zip):
  1,659,173 bytes; SHA256
  `98e9a11bd25c33fb01ed8de90129b0fc2fffa14f032232cc0cd175cb63f7b69b`.
- [Per-file manifest](https://github.com/lizaixi01/AgentCAD/blob/a3b336a4535bc3cc2a2435f4ebdc76b90f70153c/examples/autocad_benchmark/results/2026-10-30-v8/integrated/evidence-manifest.json):
  299 archived files, 62 product-source hashes, 193 upstream-source hashes,
  46 returned artifact references, and regression output (435 passed, 74 skipped).
- [Same-machine Pro reference rerun](https://github.com/lizaixi01/AgentCAD/blob/a3b336a4535bc3cc2a2435f4ebdc76b90f70153c/examples/autocad_benchmark/results/2026-10-30-v8/reference.json):
  31/31. It was reused for later product rechecks, not presented as a fresh run.
- [Independent review of the final evidence](https://github.com/lizaixi01/AgentCAD/pull/36#issuecomment-6518397926)
  and [integration review](https://github.com/lizaixi01/AgentCAD/pull/36#issuecomment-6518267178).

Earlier 9/31 and intermediate full-pass records remain in the product repo.
The fixed matrix does not cover every product limitation: architecture is
currently millimetre/orthogonal, mechanical production is limited to the
documented 2D profiles and sections, and plant lengths are preliminary
rectilinear estimates. See the pinned
[workflow contracts](https://github.com/lizaixi01/AgentCAD/blob/39fa4b55899233ab7ac5cbb52714a4767ca164c6/docs/engineering-workflows.md).

## Review request

Please review the translated playbooks and evidence, reproduce the registered
lane, and consider including AgentCAD in the runtime table and generated
matrix. Source-reviewed capability scores require a separate assessment under
the existing six-category rubric; this submission does not self-award points
or change that rubric. If a finer distinction between full-pass servers is
useful, additional common tasks or a declared common-interface efficiency
protocol would need agreement before either server is ranked on it.
