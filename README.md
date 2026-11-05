<div align="center">

# AutoCAD MCP Pro

**Production-grade AutoCAD automation for AI agents.**
Live through COM on Windows, or headless through ezdxf anywhere — one typed contract, two engines.

[![CI](https://github.com/U-C4N/Autocad-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/U-C4N/Autocad-MCP/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/autocad-mcp-pro?color=1f6feb&label=PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/autocad-mcp-pro/)
[![Downloads](https://img.shields.io/pypi/dm/autocad-mcp-pro?color=8957e5&label=downloads)](https://pypi.org/project/autocad-mcp-pro/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-1f6feb?logo=python&logoColor=white)](https://github.com/U-C4N/Autocad-MCP/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-3fb950)](https://github.com/U-C4N/Autocad-MCP/blob/main/LICENSE)

[Install](#install) · [What's new](#whats-new-in-18) · [Tools](#what-you-get) · [Engines](#the-two-engines) · [Evidence](#evidence) · [Limits](#what-this-release-is-bad-at) · [Config](#configuration) · [Changelog](https://github.com/U-C4N/Autocad-MCP/blob/main/CHANGELOG.md)

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-showcase.png" alt="ISO A3 sheet: a module-6, 24-tooth spur gear with a keyed bore, its section A-A, ISO 129 dimensions with an H7 bore fit, and an ISO 7200 title block" width="800">

<sub><b>Not a mockup.</b> Every line on this sheet was drawn by the tools this server exposes — ISO layers, involute gear geometry, DIN 6885 keyway, section A-A, ISO 129 dimensions, an ISO 286 <code>H7</code> bore fit, ISO 7200 title block — then rendered headlessly by <code>view_screenshot</code>. <code>drawing_critique</code> returns <b>0 issues</b> on it. Rebuild it with <code>python scripts/render_readme_showcase.py</code>.</sub>

</div>

> **v1.8 release snapshot:** 257 tools · 13 resources · 5 prompt templates · 7541 collected tests.
> 257 is the **registered** count. Since 1.8.0 a default install lists six over `tools/list`
> (`DISCOVERY_MODE=search`: `search_tools`, `describe_tool`, `call_tool` and the three
> `DISCOVERY_PINNED` tools), and every tool the profile and packs allow stays callable by name;
> `DISCOVERY_MODE=off` lists 253: the 252 a default profile enables (`ENABLE_3D` unset hides the five
> solids) plus `describe_tool`. `system_about` is the runtime authority.

## What's new in 1.8

1.7 made what the server draws provable. 1.8.0 makes it **remember**, finish a job in **one call** and **measure itself honestly** ([design](https://github.com/U-C4N/Autocad-MCP/blob/main/docs/superpowers/specs/2026-11-03-v1.8.0-design.md)). The new tools are `entity_modify_where`, `layer_rename`, `layer_merge`, `checkpoint_create`, `checkpoint_list`, `checkpoint_restore`, `drawing_history`, `mech_part_revise` and `pid_project`, and the new resources `autocad://critique/focuses` and `autocad://pid/project/last`; the counts are in the release snapshot above, and `system_about` is the authority.

| | What 1.8.0 adds |
|---|---|
| **It remembers** | The operation journal is durable by default (sqlite in `ACADMCP_STATE_DIR`): a reopened file continues its `document_id` and revision, a client operation id replays after a restart, and headless `drawing_undo` steps back to the newest checkpoint, also after a restart. `checkpoint_create` / `checkpoint_list` / `checkpoint_restore` keep stored states; live, `mode="in_place"` is refused with capability `checkpoint_restore_in_place` and `new_document` opens the checkpoint instead. `drawing_history` finds the value a drawing had in round 2 in two calls after a restart. Every headless DXF write is atomic, and `drawing_open(path, recover=True)` reads a damaged DXF into a new lineage whose source is protected (`recovered_source_protected`) |
| **One call, not hundreds** | `entity_modify_where` is QSELECT + CHPROP with a dry run, `layer_rename` renames every reference, `layer_merge` is LAYMRG, and `dimension_auto(selector=...)` dimensions a whole part. Measured task cells: a layer clean-up in 2 calls (1.7.0: 124) and a plate's dimensions in 2 (1.7.0: 89). A `cad_batch` that writes is all or nothing by default, and a literal handle that names no entity refuses the whole batch before anything runs (`unknown_handle`) |
| **Six tools listed, every tool callable** | `DISCOVERY_MODE=search` is the default: `search_tools`, `describe_tool`, `call_tool` and the pinned `cad_batch`, `drawing_info` and `entity_list` cost **1,026** prompt tokens idle, against 59,138 for the full `DISCOVERY_MODE=off` list. A description over 800 characters is listed short, with `Details: describe_tool.`; the whole text is one call away ([table below](#why-this-exists)) |
| **One error envelope** | Every error on every route is `{ok: false, kind, category, retryable, hint, operation_id, ...}`, typed by exception class and never by message text; the text content is what a 1.7 client read. A missing handle, path, command or file has its own kind (`not_found`, `unknown_handle`, `file_not_found`, ...). A failed `drawing_finalize` returns its `score`, `blocking` rows and `qa_coverage`, so no second QA read is needed |
| **QA that names what it did not check** | Every critique lane writes `checked` or `not_checked` with a reason, and `qa_coverage` carries them into finalize, delivery and the manifest. The `dimension_truth` focus judges a dimension by the text the sheet really shows (`dim_override_disagrees`, `dim_render_stale`, `dim_value_hidden`, ...), and `property_override` reports entities that override their layer. A lineweight of -3 is *Default*, never ByLayer. With `summary=True` a 500-issue critique is answered in 735 characters |
| **Parts revised, P&IDs as projects** | `mech_part_revise` changes a drawn part by its parameters and rebuilds its views in place, keeping its handles; a hand edit refuses it (`hand_edited_tagged_entity`), and a part drawn before 1.8.0 is `model_drift` until `overwrite=True`. `pid_line_draw(route="avoid")` goes round the symbols between its ports (`no_route`, `routing_budget_exceeded`), checked by the new `pid_line_through_symbol` focus. `pid_project` checks off-page links, cross-sheet tags and line continuity over a set of sheet files without modifying any; a `.dwg` sheet is read only on the live engine and refused headlessly with capability `dwg` |
| **DWG opens headlessly** | `drawing_open` on a `.dwg` converts it through the ODA File Converter, then AutoCAD's `accoreconsole`. With neither it is a typed capability refusal (`dwg_read`); when every route fails it is `dwg_conversion_failed` with its `attempts[]`. A save onto the `.dwg` is still refused (`dwg`): save it as `.dxf` |
| **Narrowed on request** | `READ_ONLY`, `TOOL_ALLOWLIST` / `TOOL_DENYLIST` and `MAX_TOOL_COST` refuse a call before anything runs (`policy_refused`). A writer takes only its own extensions and never a name AutoCAD or Windows loads (`path_refused`). The HTTP transport refuses an unknown Origin (403), a foreign Host (421) and a body over 8 MiB (413). `system_run_lisp` runs an AutoLISP read tier under `LISP_POLICY=read` (COM only: the headless engine has no LISP) |
| **The live engine draws where it means to** | A live seat draws in model space until `layout_set_current` names a sheet; the tab the operator clicks no longer moves it. `system_run_command` and live undo / redo wait for the command and can fail `command_incomplete`. One display refresh per COM job, and `entity_batch_create` / `entity_delete_many` run as STA jobs sliced at a quarter of `COM_CALL_TIMEOUT`. All fourteen live smokes passed for this release (AutoCAD 2026, 1.8.0): see *Live evidence* in the [changelog](https://github.com/U-C4N/Autocad-MCP/blob/main/CHANGELOG.md) |
| **Evidence** | Correctness suite 83 / 83 against v1.7.0's 61 / 83, 22 fixed and 0 regressed (`ab-v1.7.0-vs-v1.8.0.json`). Matrix v9: 47 / 47 attempted tasks of 49 (`lisp_read_tier` `environment_blocked`, `std_tables_b40` `not_run`). The full headless suite: `7540 passed, 1 xfailed`. The headless performance gate against v1.7.0: `ok: true`, ratios 0.96 to 1.06 ([`perf-v1.7.0-vs-v1.8.0.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/perf-v1.7.0-vs-v1.8.0.json); the 1.06 is the durable journal, [measured](#headless-performance)). The token budget gate exits 0 ([evidence](#evidence)) |

### One call where 1.7 needed hundreds

```text
drawing_open(path)                                 the journal continues the file's id and revision
entity_modify_where(where, set, dry_run=True)      count, would_change and a preview
cad_batch(steps, expected_revision=...)            all or nothing; a missing handle refuses the batch
dimension_auto(selector={"layer": "GEOMETRY"})     a part dimensioned in one call
drawing_critique(focus=None, summary=True)         compact; qa_coverage names every lane not checked
drawing_history(handle=..., detail="args")         what a value was in round 2, after a restart
checkpoint_restore("cp-3", mode="new_document")    back to a stored state, as its own document
```

Several defaults changed. Each has a switch back to 1.7.0's behaviour where one exists (`DISCOVERY_MODE=off`, `cad_batch(on_error="stop")`, `JOURNAL_DURABLE=off`, `LISP_POLICY=denylist`, `strict_critique=False`, `COM_REFRESH_MODE=regen`, ...), and the changelog's *Breaking at a glance* lists every one. Not in 1.8.0: no B40 feature-table row is transcribed (every size is still refused by name), and the native .NET worker is deferred to 1.9.0 by a rule fixed before any measurement (`docs/adr/0001-native-worker.md`).

## What was new in 1.7 (the previous release)

1.6 taught the server to draw whole sheets and to read other people's drawings. 1.7.0 makes what it draws **provable** and what it runs **safe** ([design](https://github.com/U-C4N/Autocad-MCP/blob/main/docs/superpowers/specs/2026-10-24-v1.7.0-design.md)). One new tool, `drawing_verify_requirements`, and three new resources; the rest is what the existing 247 tools now guarantee.

| | What 1.7.0 adds |
|---|---|
| **Requirements, measured** | A source requirement (*Ø40 bore, six Ø8 holes on a Ø100 PCD*) is bound to the real CAD feature and measured on the geometry read from the drawing: never on a dimension's label, a stored part model or the numbers that were typed in. Each requirement gets **pass / fail / unknown / unsupported / ambiguous** with target, measured, delta and the handles behind it. Two equal holes where the requirement names one are `ambiguous`, never a pick. A dimension reading 40 over geometry that measures 42 fails on the geometry, and the label is reported as its own `dimension_mismatch` finding |
| **Measured again on the saved file** | `drawing_finalize(requirements=)` and `drawing_deliver(requirements=)` reopen what they wrote and measure every requirement on the file, re-binding each feature by handle, tag or geometry. A hole moved inside an unchanged outline in the delivered file fails delivery; 1.6's parity check passed it. Parity compares the file with a second export of the same document, so content the export itself loses is caught by the requirements measured on the file, not by parity. A file checked against an open document that is not its source is never accepted (`artifact_source_mismatch`). A manifest whose artifact changed afterwards is reported stale |
| **Safe execution** | A cancelled call keeps the drawing until its worker really ends. QA reads every entity of every space (a defect at position 5,001 or on a paper layout used to read as clean), and a check that crashes says so. CSV and XLSX text never becomes a formula. NaN and infinite spec values are refused by their path |
| **Identity, revisions, previews** | Every document has an id and a content revision. On a live seat the session stays on the document it chose: a switch of AutoCAD's active tab is refused (`active_document_changed`), not followed. `cad_batch`, `drawing_refine`, `text_find_replace` and `pid_from_spec` preview exactly what they would change and apply only if nothing moved (`stale_revision`). A client operation id makes a retry replay instead of repeat |
| **The skill, as a plugin** | The premium skill is back. It is rewritten around this workflow and installs with the server as a Claude Code plugin ([below](#as-a-claude-code-plugin)), along with twenty negative eval cases |
| **Task recipes** | Five versioned recipes served as resources (`autocad://recipes/index`): dimensioned part → verify → deliver, foreign P&ID → lists, revision pair → takeoff delta, A3 sheet delivery and foreign-drawing audit. Each one runs end to end in a test |
| **Evidence** | Correctness suite 61 / 61 against v1.6.0's 48 / 61 with zero regressions, matrix v8 31 / 31, the live COM harness passing on AutoCAD 2026, and no workload slower than 1.6.0 beyond the measured noise ([evidence](#evidence)) |

### The verification workflow

```text
drawing_preflight(intent, requirements)        typed items: id, quantity, target + unit, feature, source
drawing_plan(..., persist_requirements=True)   the set travels inside the DWG/DXF
mech_part_draw(spec)                           every entity tagged with its feature
drawing_verify_requirements(use_stored=True)   measured on the document: pass / fail / unknown / ambiguous
drawing_critique(focus=None)                   drafting checks over every entity of every space
drawing_deliver(..., requirements=...)         exported, reopened, measured again on the file
drawing_verify_requirements(manifest_path=)    later: has anything in the delivery changed since?
```

A drafting score is not an acceptance. `drawing_finalize` still returns its 0–100 score, but with `requirements=` a requirement that does not pass blocks it (`failed_acceptance`), whatever the score.

<details>
<summary><b>What 1.6 added: five tracks, 154 → 247 tools</b></summary>

Five tracks of one [roadmap](https://github.com/U-C4N/Autocad-MCP/blob/main/docs/superpowers/specs/2026-09-15-v1.6-roadmap-design.md). Each track has its own design spec, correctness checks that can fail, and a live AutoCAD 2026 run of its COM paths recorded in the [changelog](https://github.com/U-C4N/Autocad-MCP/blob/main/CHANGELOG.md).

| Track | What it adds | Pack |
|---|---|---|
| **P&ID — drafter and reader** | 43 ISO 10628-2 / ISA-5.1 symbols as real blocks with ports and `TAG` attributes, port-to-port lines with ISA-5.1 classes and line numbers, `pid_graph` reading any P&ID back with a confidence per node, instrument index, line list, equipment list, `pid_from_spec` for a whole sheet in one transaction | `pid` |
| **The AutoCAD environment** | ISO-25 / ANSI dimension styles, text and leader styles, `drawing_apply_standard`, ISO 216 / ANSI page setups checked against the plotted PDF's own `/MediaBox`, five bundled templates, several open documents headlessly, portable layer states, named views, UCS, a 90-entry system-variable catalogue; on a live seat, preferences, launch and operator prompts | `core`, `settings` |
| **Mechanical parts and the sheet** | one part model and one view engine (front, side, top, section and detail views with hidden lines, cut faces and ISO 129 dimensions), ISO fasteners and bearings as blocks with attributes, ISO 21920-1 surface texture, ISO 2553 welds, ISO 5457 frames A4–A0, ISO 7200 title blocks, an ISO 7573 parts list read off the drawing with linked ISO 6433 balloons, xrefs, images, real `.dwg` | `mech`, `core` |
| **Architecture** | walls with hosted doors and windows: one engine resolves L / T / X junctions and cuts the openings. Room areas are measured from the faces the walls enclose, and rooms are read back from any plan. Door / window / room schedules are real tables; labels in English or Turkish | `arch` |
| **Understanding and takeoffs** | `drawing_understand` reads a drawing somebody else made in one call. Also `drawing_scale_check`, `drawing_diff` and `drawing_topology_check`. `pipe_takeoff` / `cable_takeoff` take the topology from the P&ID and the lengths from the layout into a Turkish, English or Russian workbook | `core`, `plant` |

</details>

`TOOL_PACKS` keeps a client from paying for the tracks it does not use ([idle cost below](#why-this-exists)).

<div align="center">

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/mech-showcase.png" alt="ISO A3 flange-coupling sheet: a hub with a full section and chain dimensions, a flange with its bolt-circle end view, two ISO 4014 bolts with ISO 4032 nuts, a parts list with linked balloons, an ISO 5457 frame with zones and an ISO 7200 title block" width="800">

<sub><b>A mechanical sheet, gated.</b> The <code>mech_assembly</code> benchmark sheet: a hub drawn from its part model with a full section and chain dimensions, a flange with its bolt-circle end view, two ISO 4014 M12×60 bolts and ISO 4032 nuts inserted as blocks, the ISO 7573 parts list <i>read off the drawing</i>, balloons linked to its rows, an ISO 5457 frame and an ISO 7200 title block. <code>drawing_critique(focus=None)</code> returns <b>0 issues</b> and the finalize score is <b>100</b>. Rebuild it with <code>python scripts/render_readme_mech.py</code>.</sub>

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/arch-showcase.png" alt="Two-room floor plan: hatched brick walls with an aerated-concrete partition, two doors, two windows, a straight stair, room labels with measured areas, exterior dimension chains, and door, window and room schedules" width="800">

<sub><b>A floor plan whose areas nobody typed.</b> <code>arch_plan_from_spec</code>'s example, drawn in one transaction: a 250 mm brick ring split by a 100 mm AAC wall, two doors, two windows, a straight stair and exterior dimension chains at 1:50. The rooms measure 27.74 and 21.99 m², read from the faces the walls enclose, and the door, window and room schedules are read off the drawing. <code>drawing_critique(focus=None)</code> returns <b>0 issues</b>. Rebuild it with <code>python scripts/render_readme_arch.py</code>.</sub>

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/pid-showcase.png" alt="P&ID sheet: a centrifugal pump, a diaphragm-actuated globe control valve, a vertical vessel with two nozzles, a flow controller bubble and an off-page connector, joined by numbered process lines and an electric signal line" width="800">

<sub><b>The same server as a P&ID drafter.</b> <code>pid_from_spec</code> placed five catalogue symbols as real blocks with <code>TAG</code> attributes and named ports and routed four port-to-port lines with ISA-5.1 classes and line numbers, all in one transaction. Then <code>pid_graph</code> read the sheet back: 5 nodes, 4 edges, 0 dangling ends, <code>confidence_min</code> 1.0. Rebuild it with <code>python scripts/render_readme_pid.py</code>.</sub>

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/pid-catalog.png" alt="Contact sheet of the 43 P&ID catalogue symbols: valve bodies with actuators, pumps and compressors, heat exchangers, vessels, instrument bubbles, line markers and connectors" width="800">

<sub><b>The catalogue, authored in code.</b> 43 ISO 10628-2 / ISA-5.1 symbols: 11 valve bodies × 6 actuators, 6 rotating machines, 4 heat exchangers, 6 parametric vessels, 7 inline items, the instrument bubble in 4 types × 5 locations, 5 line markers and 3 connectors. Every one was placed through <code>pid_symbol_insert</code>'s own path. Rebuild it with <code>python scripts/render_pid_catalog.py</code>.</sub>

</div>

## Why this exists

**A big MCP server is expensive to be connected to.** Listing the whole catalog (`DISCOVERY_MODE=off`, 253 tools) costs a client **59,138 tokens** of prompt payload before it has asked for anything. Since 1.8.0 a default install lists six tools instead - `search_tools`, `describe_tool`, `call_tool` and the three `DISCOVERY_PINNED` tools - for **1,026 tokens**, and every tool stays callable by name.

**A drafter searches for `FILLET`, not `entity_fillet`.** Before the corpus, those command names appeared in no tool name or description. That was `df = 0` against a stock index: the tools were not ranked badly, they were *absent*. The fix was data: an authored corpus of **207 AutoCAD command names and 1,479 distinct synonym phrases** covering all 257 registered tools. A test refuses to let a tool exist without one.

| Advertised surface | Tools listed | Prompt payload | Wire (`tools/list`) | 1.7.0 prompt payload | 1.7.0 wire |
|---|---:|---:|---:|---:|---:|
| `DISCOVERY_MODE=search` (default since 1.8.0, with the three pins) | 6 | **1,026** | 1,300 | — | — |
| `DISCOVERY_MODE=search`, `DISCOVERY_PINNED=""` | 3 | **220** | 230 | 455 | 503 |
| `DISCOVERY_MODE=off` (`TOOL_PROFILE=full`) | 253 | 59,138 | 78,391 | 62,471 (243 tools) | 89,350 |
| `DISCOVERY_MODE=off`, `TOOL_PACKS=core,mech` | 207 | 46,767 | 62,425 | 47,955 (198) | 69,391 |
| `DISCOVERY_MODE=off`, `TOOL_PACKS=core,arch` | 204 | 44,901 | 60,289 | 46,679 (196) | 67,974 |
| `DISCOVERY_MODE=off`, `TOOL_PACKS=core` | 192 | 40,722 | 55,169 | 41,902 (184) | 61,546 |
| `DISCOVERY_MODE=off`, `TOOL_PROFILE=lean` | 68 | 16,182 | 21,189 | 17,945 (66) | 25,146 |
| Server instructions (the longest text the builder writes) | — | 199 | 199 | 461 | 461 |

> [!NOTE]
> Measured on the 1.8.0 release tree with `python -m benchmarks.token_suite` (idle lane, over the real in-memory server; [`token-cost.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/token-cost.json)). The 1.7.0 columns are the v1.7.0 tag measured the same way with this release's lock (`benchmarks/token_budget.json`, `v1.7.0_measured`); 1.7.0's own README quoted the wire column on its older lock (89,226 / 61,422 / 25,066 / 503). Tokens are offline ratio estimates, not tokenizer counts, and they are the *uncached* cost: prompt caching amortises the idle term. The prompt payload is the `tools` array a client forwards to the model; the wire column is the whole `tools/list` result, the basis of every figure up to 1.7.0. In `off` mode the list carries `describe_tool` too, and a description over 800 characters is listed as its leading paragraphs plus `Details: describe_tool.`, the full text one `describe_tool` call away. `benchmarks/token_suite.py --tokenizer anthropic` counts for real.

## Install

```bash
pip install autocad-mcp-pro     # or: uvx autocad-mcp-pro
autocad-mcp                     # stdio MCP server, backend auto-selected
```

```bash
AUTOCAD_MCP_BACKEND=ezdxf autocad-mcp    # portable DXF engine, no AutoCAD needed
AUTOCAD_MCP_BACKEND=com   autocad-mcp    # live AutoCAD (needs the [com] extra)
```

| Extra | Pulls in | For |
|---|---|---|
| *(none)* | `fastmcp`, `ezdxf`, `pydantic` | Headless DXF on any OS — **no rendering** |
| `[com]` | `pywin32`, `Pillow` | Live AutoCAD control + window capture |
| `[pdf]` | `matplotlib` | PDF export and headless PNG, any platform |
| `[office]` | `openpyxl` | XLSX workbooks (takeoffs, `data_extract`); CSV is always written |
| `[full]` | everything above | Development and CI |

> [!NOTE]
> The bare install **cannot draw pixels** — `ezdxf.addons.drawing` imports Pillow unconditionally, so every render path needs it. Add `[pdf]` for images on Linux or macOS.

### As a Claude Code plugin

The server and the premium skill install together. The skill teaches the measured workflow: requirements → geometry → verified on the CAD → saved → reopened → delivered.

```bash
claude plugin marketplace add U-C4N/Autocad-MCP
claude plugin install autocad-mcp-pro@autocad-mcp
```

The plugin runs `uvx --from "autocad-mcp-pro[com,pdf,office]==1.8.0" autocad-mcp`, which is this release at exactly the plugin's version, so it needs [uv](https://docs.astral.sh/uv/). Set `AUTOCAD_MCP_SOURCE` to a checkout (`C:/path/to/Autocad-MCP[com,pdf,office]`) to run that code instead. The plugin starts the server in search mode (`DISCOVERY_MODE=${AUTOCAD_MCP_DISCOVERY:-search}`); set `AUTOCAD_MCP_DISCOVERY=off` for the full catalog. Already running the server yourself? Keep one: turn the plugin's server off in `/mcp`, or copy `.claude/skills/autocad-mcp-premium/` into your `.claude/skills/` instead. [`plugin/README.md`](https://github.com/U-C4N/Autocad-MCP/blob/main/plugin/README.md) has the details.

<details>
<summary><b>Wire it to an MCP client</b></summary>

```json
{
  "mcpServers": {
    "autocad": {
      "command": "autocad-mcp",
      "env": {
        "AUTOCAD_MCP_BACKEND": "auto",
        "ALLOWED_PATHS": "C:\\Users\\you\\Documents\\AutoCAD",
        "TOOL_PROFILE": "full",
        "TOOL_PACKS": "all",
        "DISCOVERY_MODE": "search"
      }
    }
  }
}
```

It works with Claude Desktop, Cursor, or any stdio MCP host. For HTTP, run `autocad-mcp --transport http --port 8000`. It binds loopback only, unless remote HTTP is explicitly enabled **and** a bearer token is set.

</details>

## What you get

| Area | What it does |
|---|---|
| Drawing lifecycle | create, open (a damaged DXF with `drawing_open(recover=True)`, which reports every fix and protects the source; a DWG headlessly through the ODA File Converter or AutoCAD's `accoreconsole`), save *(atomic headlessly: a checked temporary file renamed over the target)*, export DXF/PDF, audit *(repairs)*, purge, undo/redo *(reporting `verified`)*. A reopened file continues its document id and revision; a file changed outside the server says `external_change`, and one the server ended before saving says `lost_since_save` |
| Durability & history | headless, every save, open, new drawing, rollback batch and `checkpoint_create(label)` keeps a gzip checkpoint in `ACADMCP_STATE_DIR`; live AutoCAD keeps one only at `checkpoint_create`, at each `transaction_begin` and before `entity_delete_many` / `drawing_purge` / `text_find_replace` - not at a save, an open or a new drawing. The headless `drawing_undo` steps back to it **after a restart too** (`granularity` save / batch / label). `checkpoint_list` / `checkpoint_restore(mode="in_place"\|"new_document")` (an in-place restore is itself undoable; live AutoCAD restores as a new document). `drawing_history(label=, handle=, query=, detail=)` reads what was done to the drawing, with which arguments, across restarts: label a round with `_meta.label` or `cad_batch(label=)`, then read its value back in two calls. `drawing_diff(old_checkpoint=, new_checkpoint=)` compares checkpoints, with feature-payload and dimension-measurement changes |
| Geometry | lines, arcs, polylines (with `bulges`), splines, hatches, trim/extend/fillet/chamfer, handle-preserving edits: `entity_edit_geometry` moves circle, line and arc geometry, rewrites a polyline's `points` / `bulges` (the vertex count may change) and moves a TEXT/MTEXT by `x` / `y`; `entity_edit_text` edits content, height, rotation and position |
| Set-based edits | `entity_modify_where(where, set)` restyles every entity a filter matches in one call (QSELECT + CHPROP: layer globs, type, colour, linetype, lineweight, region, block); `dry_run` returns the count, the first 20 handles and a `preview`, and an `expect_count` the drawing no longer matches is refused `stale_revision`. `layer_rename` renames a layer and every reference to it; `layer_merge` (LAYMRG) moves the sources' entities in model space, every sheet and every block definition onto a target and names the block definitions it touched. Entities on locked layers are skipped and reported |
| Annotation | ISO 129 toleranced dimensions, ISO 286 fits (`fit="H7"`, interference shafts r/s/t/u included), TABLE, MLEADER, GD&T frames and datums (ISO 1101). `dimension_auto(selector={"layer": "GEOMETRY"})` dimensions a whole part in one call - polyline edges per side, hole diameters, arc radii, the overall size - with `include`, `group_equal` (`6× Ø11`), `skip_existing` (on by default: a rerun draws nothing) and `dry_run`; whatever it cannot dimension is listed in `omitted` with its reason. A headless `dimstyle_modify` redraws the dimensions using the style (`rerender=False` leaves them stale, as 1.7 did) |
| Engineering generators | involute gears (front + section A-A), DIN 6885 keyed bores, ISO A3 title block |
| Mechanical parts | one part model (segments or outline + typed features) and one view engine: front / side / top / section / detail with hidden lines, ISO 128-50 cut faces and ISO 129 dimensions. ISO 4014/4017/4032/7089/4762 fasteners and ISO 15 bearings insert as real blocks with attributes. `mech_part_revise(part_id, changes)` changes a drawn part's parameters by path (`{"features.bore.diameter": 24}`) and rebuilds its views in place, keeping the handles of the lines, circles and arcs it edits; a hand-edited tagged entity or a part drawn before 1.8.0 is refused unless `overwrite=True`, and each revision joins a model history that travels with the file (`mech_part_inspect(history=True, param=...)`, `drift=True` for what no longer matches the model). DIN 471/472, DIN 509, DIN 332 and ISO 3601-2 features have their drawing code and, since 1.8.0, a double-entry transcription pipeline, but *their tables still ship empty* (see [limits](#what-this-release-is-bad-at)). Also ISO 21920-1 surface texture, ISO 2553 welds and ISO 128-40 section lines |
| Sheet & delivery | ISO 5457 frames A4-A0 with zones and trim marks, ISO 7200 title blocks for every size, revision blocks with clouds, ISO 7573 parts lists with ISO 6433 balloons linked by XDATA, CSV/XLSX extraction, xrefs and images on both engines, real `.dwg` on a live seat |
| Architecture | one plan model (walls with hosted doors and windows) and one engine that resolves L / T / X junctions, cuts openings and draws poché by material. Room areas are measured from the faces the walls enclose, never typed, and rooms are read back from any plan with a confidence. Door / window / room schedules are real TABLEs. Also a furniture and sanitary catalogue at nominal sizes, a structural grid, north arrow, section / level / elevation marks, exterior dimension chains, and English or Turkish labels (`lang="tr"`, decimal comma) |
| Understanding & takeoffs | `drawing_understand` reads a drawing somebody else made in one call: declared vs inferred units, robust extents and the outlier behind them, plan copies, each layer's discipline and service in five languages, tags, rooms. `drawing_scale_check` says whether a P&ID is to scale against its layout. `pipe_takeoff` / `cable_takeoff` take the topology from the P&ID and the lengths from the layout (rectilinear MST, statuses A/B/C, power never invented) into a Turkish / English / Russian workbook. `drawing_diff` compares revisions; `drawing_topology_check` finds dangling ends, near misses and crossings |
| P&ID | catalogue blocks with ports and tags (ISO 10628-2 / ISA-5.1), port-to-port lines with ISA-5.1 classes and line numbers, `pid_graph` reads any P&ID back with confidence, instrument index / line list / equipment list, `pid_from_spec` one-call sheets. An `avoid` line (`pid_line_draw(route="avoid", clearance=, keep_out=)`, `pid_from_spec`'s `sheet.routing: "avoid"`) goes round the vessels between its ports, and `obstacle_hits` / the `pid_line_through_symbol` focus say when a line does not. `pid_project` checks a project of several sheet files without modifying any: off-page links matched across files, cross-sheet duplicate tags, line-number continuity, and the three lists merged with a `sheet` column (last result: `autocad://pid/project/last`) |
| Styles & standards | ISO-25 / ANSI dimension styles, ISOCP / ROMANS text styles, ISO / ANSI leader styles from authored presets; `drawing_apply_standard("iso")` sets all of it plus units and the `mech` layers in one call; `changed` reports only what moved |
| Discovery | the default since 1.8.0 (`DISCOVERY_MODE=search`): a client lists `search_tools`, `describe_tool`, `call_tool` and the `DISCOVERY_PINNED` tools (`cad_batch`, `drawing_info`, `entity_list`), and can still call every tool by name. `search_tools` is ranked over an AutoCAD command and synonym corpus — `FILLET`, `BPOLY`, `QSELECT`, `WBLOCK`, `OVERKILL`, `CHSPACE` each rank **#1** among the 252 tools a default install searches (the v9 `tool_discovery` task); `describe_tool` returns one hit's full input schema, risk, pack and its docstring's own examples, or up to 25 with `names=[...]`. The server instructions name only the tools the client can see and teach batching first |
| Batching | `cad_batch` runs a step list in one round trip and, since 1.8.0, is all or nothing by default (`on_error="auto"`: a batch that writes is rolled back on a failure; `on_error="stop"` keeps the steps before it). A literal handle that names no entity refuses the whole batch before anything runs (`unknown_handle`); the result carries `changes` {added, deleted, counts} and `label=` names it in the history. `entity_delete_many` deletes all of its handles or none. `fields=` projects 11 result-heavy tools |
| Paper space | tab lifecycle, viewports, `entity_change_space` (CHSPACE), `drawing_export_pdf(layout=…)` |
| Page setup & templates | ISO 216 / ANSI Y14.1 sheets, ctb catalog, `page_setup_apply` on both engines, and `batch_plot` with every sheet size read back from its PDF's `/MediaBox`. Five bundled templates, built by the server's own tools and pinned reproducible: `drawing_new(template="iso_a3_mech")`, `iso_a1_arch`, `iso_a3_pid`, `ansi_b_mech`, `ansi_d_arch`. Save any drawing as a template (`.dwt` on live AutoCAD; `dwt_write` refused headlessly) |
| Environment | several open documents headlessly, portable layer states (`ACADMCP_LAYERSTATES` XRECORDs — in the file, not in AutoCAD's Layer States Manager), named views, UCS *(tool coordinates stay WCS)*, a system-variable catalog with ranges, document properties. On a live seat: launch/attach, preferences, and an operator prompt / pick / select, where ESC is `cancelled`, not an error |
| Selection | window vs crossing stated back to the caller; a polygon tested against its own shape, not its bounding box |
| Boundaries | `boundary_trace` (BOUNDARY/BPOLY) chains loose edges into one closed polyline, arcs kept as bulges *(headless)* |
| Measurement | `analysis_measure_entity` measures what is *in* the drawing, by handle |
| Hatch depth | gradients, in-place edits, typed edge boundaries *(headless)*, island styles |
| Annotation objects | WIPEOUT, REVCLOUD, MTEXT background masks *(headless)*, text find/replace |
| 3D solids | `solid_box/cylinder/extrude/revolve/boolean` on live AutoCAD (`ENABLE_3D=true`) |
| Quality loop | `drawing_preflight` → `drawing_plan` → `drawing_critique` → `drawing_refine` → `drawing_finalize` (0–100 score); the general focuses (`iso128`, `layer_color`, `dim_overlap`, `untrimmed_corner`, `duplicate_entities`, `construction_left`, `property_override` (an entity on an engineering layer that overrides the layer's lineweight, colour or linetype; `drawing_refine` sets it back to ByLayer), `dimension_truth` (a DIMENSION whose override, DIMLFAC, rendered block, group 42, prefix or visibility disagrees with its geometry or its style; `drawing_refine` redraws a stale block and leaves the lies to a person), `gdt`) plus 20 domain critique focuses (P&ID 7 - with `pid_line_through_symbol`, a line through a symbol that is not its end - mechanical 7 - with `model_drift`, a drawn part whose tagged entities or views no longer match its stored model - architecture 3, topology 3), each silent on a drawing outside its domain; `autocad://critique/focuses` lists every one with what it reports. Every check reads every entity of every space, and a check that fails says so. Every check also says what it looked at: `drawing_critique(report=True)` returns the coverage per layout, and `drawing_finalize` / `drawing_deliver` carry `qa_coverage`, naming each lane that was **not** checked and why (`no_viewport_scale`, `engine_live_document_template`, ...); `strict_critique=True` fails on those. `drawing_critique`, `validation_check` and `drawing_topology_check` take `summary` / `limit` and answer `{total, by_focus, by_severity, issues[:limit]}`, `total` always complete. A failed `drawing_finalize` returns its `score` and `blocking` rows (`detail="full"` for all of them) |
| Verification | `drawing_verify_requirements` binds typed source requirements to the real features (handle, feature tag, geometry or DIMENSION) and measures them on the drawing or on a saved file: distance, radius, diameter, angle, concentric, parallel, perpendicular, tangent, counts and absence, each **pass / fail / unknown / unsupported / ambiguous** with its evidence, as JSON or Markdown. The set can be stored in the drawing itself |
| Delivery | `drawing_deliver`: DXF/PDF/PNG + SHA-256 manifest, reopen parity against a second export over every space and block definition (its `scope` says what that can and cannot see), and with `requirements=` every requirement measured again on the reopened file; the open document is never re-pointed at the export |
| Safe edits | documents carry an id and a content revision; `cad_batch` / `drawing_refine` / `text_find_replace` / `pid_from_spec` preview exactly and apply only against an unchanged revision; a client operation id replays a retried call instead of repeating it, also after a restart (the journal is durable, in `ACADMCP_STATE_DIR`) |
| Errors | every failure on every route is one typed envelope: `{ok: false, kind, category, retryable, hint, tool, backend, operation_id, message}` beside the unchanged sentence. `retryable` is true only when nothing was written; a refused raw command lists the typed tools that do the same job (`alternatives`) |
| Security | `READ_ONLY`, `TOOL_ALLOWLIST` / `TOOL_DENYLIST` and `MAX_TOOL_COST` narrow what a client may reach: a refused tool is not advertised, and a call to it - direct, through `call_tool`, or as a `cad_batch` step - is refused `policy_refused` before anything runs (`system_status.policy` says what is in force). Every writer accepts only its own extensions and none writes a file AutoCAD or Windows loads (`acad*.lsp`, `.fas`, `.scr`, `.dll`, `acad.pgp`, ...), even inside `ALLOWED_PATHS`. Over HTTP, an unknown Origin is 403, a foreign Host 421, a body over 8 MiB 413, tokens are compared in constant time, and `MCP_AUTH_TOKEN_READONLY` issues a read-only token. `system_run_lisp` runs the AutoLISP read tier (`vla-get-*`, `entget`, `mapcar` over a `lambda`, ...) under `LISP_POLICY=read`, and refuses the rest with `symbol`, `line`, `column` and the typed `alternatives` |
| Session metrics | `system_status(metrics=True)` reports this connection's calls, `by_tool`, `result_bytes`, `errors_by_kind` and p50 / p95 ms, and on a live seat each COM job's queue / guard / work / refresh / counter ms; off by default. `system_status.operations` is a short summary (`{count, last, unknown_commit}`); `drawing_history` lists the calls |
| Live speed | the live engine refreshes the display once per COM job (`Update()` of the entity for a single create, one Regen otherwise; `COM_REFRESH_MODE=regen` for a seat where `Update()` does not paint), `entity_batch_create` / `entity_delete_many` run as one STA job sliced inside `COM_CALL_TIMEOUT`, and the corner operations read only the target block's new entities. `benchmarks/perf_com.py` measures it against v1.7.0 on a live seat: on AutoCAD 2026 three of its four targets are missed (see *The live speed-ups miss three of their four targets* below) |

<sub>257 tools in 28 groups. <code>TOOL_PACKS=core</code> hides these from a client that needs none of them: the ten <code>pid_*</code> tools, the 22 environment tools, the 15 mechanical tools, the 12 architectural tools and the two plant takeoffs. <code>TOOL_PACKS=core,mech</code> keeps the mechanical ones, <code>TOOL_PACKS=core,arch</code> the architectural ones, and <code>TOOL_PACKS=core,plant</code> the takeoffs. There are also 13 resources that cost nothing in the tool budget (<code>autocad://drawing/info</code>, <code>layers</code>, <code>blocks</code>, <code>entities/stats</code>, <code>entities/{layer_name}</code>, <code>system/status</code>, <code>pid/symbols</code>, <code>pid/project/last</code>, <code>standards/isa51</code>, <code>standards/coverage</code>, <code>critique/focuses</code>, and the task recipes <code>recipes/index</code> and <code>recipes/{recipe_id}</code> - seven recipes, among them <code>layer_cleanup</code> and <code>dimension_part</code>, each one <code>cad_batch</code>) and 5 prompt templates.</sub>

**Three rules worth knowing.**

- Every coordinate in and out of a tool is WCS on both engines. The one exception is TEXT `rotation`, which stays in the entity frame: a mirrored TEXT is mirror-imaged, and no scalar angle expresses that.
- Never read vertices back and shoelace them. That silently loses **28.2%** of the area on a semicircular edge. `analysis_measure_entity(handle)` reads the real geometry and states its own accuracy.
- A lineweight of **-3 is Default** (the LWDEFAULT setting), not ByLayer: ByLayer is -1 and ByBlock -2. Every lineweight input also takes the names `"ByLayer"`, `"ByBlock"` and `"Default"`, a layer refuses ByLayer and ByBlock by name, and entity rows carry `lineweight_name`.

## The two engines

One contract across 23 modules in `backends/contracts/`. `@capability(key, reason=…)` supplies a default that *refuses*, and a test holds both backends' capability key sets equal. There are **45 capability keys**. Read `system_capabilities` at runtime rather than trusting the table: several depend on what the machine has installed (matplotlib, openpyxl, the ODA File Converter).

| Capability | COM (live AutoCAD) | ezdxf (headless) |
|---|:---:|:---:|
| Live document control | ✅ | — |
| Cross-platform, no AutoCAD | — | ✅ |
| Transactions and rollback | ✅ | ✅ |
| Paper-space layouts + viewports | ✅ | ✅ |
| Viewport model-content rendering | ✅ | ✅ ᵐ *(no borders)* |
| Selection window / crossing / polygon | ✅ | ✅ |
| Entity area by handle | ActiveX `.Area` | Analytic, bulges included |
| HATCH filled area (islands subtracted) | AutoCAD's own number | Loops walked, `hatch_style` reported |
| HATCH loops and DIMENSION fields in `entity_get` | ✅ read from a `Document.Export` snapshot | ✅ |
| A session bound to its document | ✅ an active-tab switch is refused, `view_screenshot` included; a restarted AutoCAD is a new document (`application_changed`); the operator's object edits move the revision | — *only the server switches documents* |
| Where new geometry goes | model space until `layout_set_current` names a sheet; the layout tab the operator clicks never moves it | model space until `layout_set_current` |
| Raw commands | `system_run_command` waits for the command (`COMMAND_DEADLINE_S`) and reports `created` entities and `last_prompt`; a command left at a prompt is `command_incomplete` | — *refused by name* |
| REGION / 3DSOLID area | ✅ | — *ACIS is opaque to ezdxf* |
| 3D solids | ✅ *with `ENABLE_3D=true`* | — |
| DWG read | ✅ native | `drawing_open` converts it through the ODA File Converter, then AutoCAD's `accoreconsole` (`ACCORECONSOLE_PATH`), if installed; otherwise refused `dwg_read`. The opened drawing is saved as `.dxf` |
| DWG write | ✅ `SaveAs` | via the ODA File Converter, if installed |
| `.dwt` templates | ✅ | DXF only — `dwt_write` refuses |
| Reading a foreign drawing (`drawing_understand`, takeoffs) | ✅ one `Document.Export` snapshot; the document is never renamed | ✅ |
| XLSX workbooks | ✅ ᵒ | ✅ ᵒ |
| Preferences · launch · operator prompts | ✅ | — *refused by name* |
| WIPEOUT · MTEXT background colour | — *verified absent, AutoCAD 2026* | ✅ |
| REVCLOUD · BPOLY · typed hatch edges | — *no ActiveX member* | ✅ |
| CHSPACE | — *unverified on a live seat* | ✅ ᶜ |
| Undo history | ✅ AutoCAD's own; a checkpoint restores as a new document (`checkpoint_restore_in_place` refused) | durable checkpoints, also after a restart; per-call steps opt-in with `EZDXF_UNDO_DEPTH` |
| TABLE and MLEADER | Native | Portable composite |
| Screenshots and PDF | Window capture | Matplotlib ᵐ |
| A region screenshot (`view_screenshot(region=[x0, y0, x1, y1], max_px=)`) | ✅ the `Document.Export` snapshot drawn by the headless renderer (`source: "export_snapshot"`) | ✅ ᵐ |

<sub>ᵐ Needs matplotlib (<code>[pdf]</code>/<code>[full]</code>). Without it, <code>png</code>, <code>pdf</code>, <code>viewport_render</code> and <code>handle_overlay</code> report unsupported headlessly.<br>
ᵒ Needs openpyxl (<code>[office]</code>/<code>[full]</code>). Without it, XLSX is refused with <code>xlsx_write</code> and CSV is still written.<br>
ᶜ Headless CHSPACE has four restrictions, named in its capability reason: top-view untwisted viewports only; dimensions refused unless frozen; ACIS, proxy and table entities refused; viewport clipping reported rather than applied.</sub>

> [!IMPORTANT]
> The COM paths run against a **live AutoCAD 2026** through eight asserting smoke scripts (`scripts/smoke_*_com.py`) on one harness (`scripts/live/harness.py`: attach only, its own documents, an active-document guard). The 1.7.0 runs passed **129 / 129** checks on the close-out commit `13499fe`; their JSON is committed under [`benchmarks/results/live/`](https://github.com/U-C4N/Autocad-MCP/tree/main/benchmarks/results/live). These runs catch what no headless test can: ActiveX refuses `entity.Layer` on a missing layer where ezdxf quietly creates it, calls a block reference `BLOCKREFERENCE` rather than `INSERT`, and, once pywin32's type-library cache exists, hands every listed entity back as a bare `IAcadEntity` with no endpoints. Each of those broke a tool that passed every headless test.

## Evidence

Everything below is produced by scripts in [`benchmarks/`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/) and published as JSON under [`benchmarks/results/published/`](https://github.com/U-C4N/Autocad-MCP/tree/main/benchmarks/results/published).

### Against the other AutoCAD MCP servers

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-benchmark.svg" alt="Source-reviewed capability leaderboard: U-C4N/Autocad-MCP 93, varavista 84, beiming183-cloud 83, best-cad-mcp 74, multiCAD-mcp 73, puran-water 70, NCO-1986 68, daobataotie 35, Easy-MCP-AutoCad 30" width="800">

A fixed 100-point rubric applied to nine public AutoCAD MCP servers ([`source_review.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/source_review.json)). Stars and raw tool counts score nothing. 1.6.0 is scored **per category, with the evidence for every point withheld**:

| Category | Weight | 1.6.0 | Points withheld for |
|---|---:|---:|---|
| Functional CAD coverage | 25 | 24 | no electrical schematics, image-to-CAD tracing, Plant 3D, obstacle-avoiding routing or headless REGION / 3DSOLID |
| Correctness and delivery | 20 | 19 | a headless DIMENSION does not re-render; a takeoff on a real P&ID still needs the engineer's scope decisions |
| Backends and platforms | 15 | 14 | BricsCAD, ZWCAD and GstarCAD unverified |
| Engineering production | 15 | 14 | four DIN / ISO feature tables ship empty |
| Tests and maintenance | 15 | 14 | headless entity creation slower than v1.4.0 |
| Security and operations | 10 | 8 | `ALLOWED_PATHS` unscoped by default; command filtering is a denylist |
| **Total** | **100** | **93** | |

> [!NOTE]
> Read this as a documented self-assessment, not an independent review: this repository applies the rubric to itself and to the others. The competitor rows are their July 2026 snapshots and were not re-reviewed for this release, and the best of them, varavista/autocad-mcp, scored 84. 1.4.0 was given one overall 95. This review is stricter and scores each category apart. The rubric was not re-applied for 1.7.0: the 1.6.0 column stands as it was published.

A second lane runs tasks instead of reading source. Every task in the fixed-task matrix can fail, and its artifacts are verified by re-opening the DXF, never by trusting a response. 1.8.0's default is matrix v9, **49 tasks** (v8's 31 plus 18). On the headless engine this server passes **47 / 47** of the tasks it attempts ([`lanes-v9/autocad-mcp-pro.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/lanes-v9/autocad-mcp-pro.json)). The other two are not counted: `lisp_read_tier` is `environment_blocked`, because AutoLISP runs only in AutoCAD, and `std_tables_b40` is `not_run`, because every B40 table ships empty in 1.8.0 (deferred to 1.8.1). `dwg_headless_roundtrip` passed through AutoCAD 2026's `accoreconsole` on the machine that ran it; a machine with neither ODA nor `accoreconsole` reports it `environment_blocked`. AgentCAD (`lizaixi01-agentcad`), whose published comparison nine v9 tasks answer, has no adapter here, so its lane is `not_run` on every task, never a zero. The two competitors with adapters, pinned at their July 2026 commits, passed 5 / 10 and 4 / 10 of the ten v2 tasks they were run on. The tasks added since were never put to them, so their lanes record them as `not_run` or `environment_blocked`, never as zero. The chart is still the v8 render (31 tasks; v8 31 / 31):

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-taskmatrix.svg" alt="Task matrix v8: autocad-mcp-pro passes all 31 tasks; beiming183 and puran-water were run on the first ten, with the later twenty-one not run" width="800">

### Correctness — every release re-proves itself

83 deterministic headless checks against the previous tag and the current tree. Each runs in its own subprocess, so a hard crash counts as a miss rather than killing the run.

| Version | Checks passing | Pass rate | Fixed | Regressed |
|---|---:|---:|---:|---:|
| v1.7.0 *(baseline)* | 61 / 83 | 73.5 % | — | — |
| **v1.8.0** *(this release)* | **83 / 83** | **100 %** | 22 | **0** |

The 61 checks v1.7.0 was released on all still pass. Twenty-two rows are fixed ([`ab-v1.7.0-vs-v1.8.0.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/ab-v1.7.0-vs-v1.8.0.json)). Six of them are the first phase's, one per W1 item of 1.8.0: four reproduce a 1.7.0 defect on v1.7.0 (`fail → pass`), two call a method 1.7.0 does not have (`miss → pass`):

| Check | Verified against |
|---|---|
| `lineweight_truth` | an entity created with lineweight -3 reports `lineweight_name: "Default"`, never ByLayer; `"ByLayer"` by name writes -1 and reads back as ByLayer; a layer asked for ByLayer is refused before anything is written |
| `error_envelope_routes_agree` | a missing handle (`not_found`) and a path outside `ALLOWED_PATHS` (`path_refused`) carry the same kind, category and `retryable` as a direct call and as a `cad_batch` step |
| `journal_replay_after_kill` | `cad_batch(operation_id=…)` on one server, `Popen.kill()`, the same id and steps on a fresh process answer `replayed: true` with the same entity count; other steps under that id are `operation_conflict` |
| `atomic_save_untouched_on_failure` | a save whose target is held open by another process is refused `save_locked`: the target's bytes are identical, no `.tmp` and no pending marker is left, and the save after release writes the drawing |
| `lineage_revision_continues` | open, add three lines, save, close, reopen the unchanged file in a fresh backend: the same `document_id` and the same revision, never back to 0 |
| `region_render_applied` | `view_screenshot(region=[0,0,100,50], max_px=400)` answers `applied: true`, a 400 × 200 PNG (read from its own IHDR) and one `px_per_unit` on x and y; `view_zoom_and_screenshot` with four corners applies its window headlessly |

The other sixteen are the second and third phases' (twelve W2, four W3). Fourteen answer `fail` on v1.7.0; `dimension_truth_focus` and `b40_between_rows_refused` call what 1.7.0 does not have (`miss → pass`):

| Check | Verified against |
|---|---|
| `qa_coverage_not_checked` | a frame line on `Layout1` with no viewport onto the model: the critique report names that layout's legibility lane `not_checked` (`no_viewport_scale`), finalize carries the row in `qa_coverage`, and `strict_critique=True` fails `critique_gate_failed` naming it |
| `dimension_display_truth` | a 19.6 mm dimension after DIMDEC 2 → 0 shows "20" in its `*D` block and in `bind_dimension`, with `display_rounding` 0.4 and no mismatch |
| `dimension_truth_focus` | an override lie, a doubled Ø and a stale render are exactly `dim_override_disagrees`, `dim_double_prefix` and `dim_render_stale`; `drawing_refine` redraws the stale one and leaves the other two |
| `policy_read_only_batch` | under `READ_ONLY`, a one-step `entity_create_line` batch is `policy_refused` (`read_only`) and neither the revision nor the entity count moves |
| `write_path_autoload_refused` | `data_extract` to `acaddoc.lsp` and `drawing_save_as` to `acad.lsp` are both `path_refused`, nothing written |
| `dimension_auto_one_call_part` | one `dimension_auto(selector={"layer": "GEOMETRY"})` on a 120 × 80 plate draws 4 edge dimensions (120, 80, 120, 80), six Ø11 and two R8, names the ellipse in `omitted`, and a second call draws nothing |
| `property_override_focus` | a line on GEOMETRY given colour 1 and lineweight 30 is named by exactly `lineweight_not_bylayer` (error) and `color_not_bylayer` (warning) with only its handle, a clean line by nothing, and `drawing_refine` puts it back to colour 256 and ByLayer |
| `search_default_lists_pins_and_batch_first` | with nothing set, a client lists exactly `search_tools`, `call_tool`, `describe_tool`, `cad_batch`, `drawing_info` and `entity_list`; an unlisted `entity_create_line` still runs on a direct call; `describe_tool(names=[...])` answers two cards in one call; the instructions name `cad_batch`, nothing unlisted and no `titleblock_apply_iso_a3` |
| `undo_after_restart` | a real `python server.py` draws, saves, moves, saves and is killed; a new process opens the file and one `drawing_undo` puts the line back at (0, 0)-(10, 0) with `granularity: "save"`; a killed unsaved session leaves the file as saved and the next open reports `lost_since_save.count == 1` |
| `atomic_batch_default` | `[entity_move(L), entity_delete("DEAD")]` with no `on_error` is refused `unknown_handle`, `executed: 0`, L unchanged; the run-time twin (`entity_edit_text` on a LINE) is rolled back by the default with `atomicity.verified` true |
| `delete_many_all_or_nothing` | `entity_delete_many([A, "DEAD"])` does not answer `ok: true` and A survives; a delete made to fail mid-call (a locked layer) answers `ok: false` with A, B and C all still in the drawing |
| `history_round2_value` | three rounds of a plate (hole 10, 12 labelled `round 2`, 16) drawn by a real server that is then killed; a new process answers 12 with `drawing_open` + `drawing_history(label="round 2", detail="args")`; an unknown label is refused `invalid_args` naming the labels; an unlabelled round is read from the checkpoint diff |
| `pid_project_links` | two P&ID sheet files: a matched link whose line numbers disagree, an orphan, a direction mismatch and a pump on both sheets are each reported once with the sheet and the handle; 51 sheets are refused by name |
| `b40_between_rows_refused` | every empty B40 table refuses a lookup naming itself; on a synthetic double entry (round numbers, not DIN values) a listed size draws exactly its row, a size between two rows is refused with both neighbours named and nothing drawn, and an entry B one value off entry A is refused by path |
| `mech_revise_keeps_handles` | a Ø60 × 30 bushing revised to a Ø24 bore and length 40: every LINE / CIRCLE / ARC edited in place keeps its handle, the end view's bore circle is r = 12, the signatures equal a fresh draw of the hand-written revised part; a tagged line moved 1 mm by hand refuses the revision `hand_edited_tagged_entity` with nothing changed |
| `pid_avoid_no_symbol_crossing` | a pump discharging into a far vessel with another vessel between them: with `sheet.routing: "avoid"` the polyline enters no symbol's box but its two ends - measured on each INSERT's own ezdxf bounding box, not by the server's focus - and the graph read back holds no dangling end |

<details>
<summary><b>The thirteen checks 1.7 added</b></summary>

The 48 checks v1.6.0 was released on all still passed on v1.7.0. Thirteen rows were fixed in 1.7.0. Four are reliability defects 1.6.0 shipped with, each reproduced on v1.6.0 (`fail → pass`) and fixed in 1.7.0; the other nine are the roadmap's acceptance scenarios, which 1.6.0 could not even attempt (`miss → pass`: the verification engine they call is new in 1.7.0):

| Check | Verified against |
|---|---|
| `cancel_never_overlaps` | a call cancelled while its worker runs keeps the document until that worker ends: the next call runs after it, never beside it |
| `qa_scan_finds_entity_5001` | a construction line placed as the 5,001st entity is found by `drawing_critique`; the scan used to stop at 5,000 |
| `export_text_never_formula` | `=1+1`, `@x` and a leading tab come back from the parts-list CSV with a leading `'` and from the XLSX as string cells; a `-12.5` mass stays a number |
| `spec_refuses_non_finite` | a NaN vessel width, an infinite height and a NaN symbol scale in a `pid_from_spec` dry run are each refused by their path |
| `verify_t05_stale_preview_no_silent_mutation` | T05: after a `cad_batch` preview the operator moves the line in CAD; the apply is refused `stale_revision` and the line keeps the operator's (3, 3), not (8, 3); a server edit is refused too, an undisturbed preview applies |
| `verify_t09_diameter_under_its_label` | T09: a Ø39.8 circle under a dimension reading "Ø40" fails its Ø40 requirement with delta −0.2; the label is a `dimension_mismatch` finding, never the measurement |
| `verify_t10_moved_circle_against_its_model` | T10: a hole moved 2 mm after `draw_part`; the stored part model still says x = 60, the requirement measured on the circles says 22, not 20, and fails |
| `verify_t11_inner_hole_moved_caught_on_reopen` | T11: an inner hole moved inside the saved file's outline (same counts, same extents) fails its 20 mm pitch with 22 on the file, re-bound and opened alone |
| `verify_t12_equal_holes_ambiguous` | T12: "the Ø10 hole" among two equal ones is `ambiguous` with both as candidates and no handle chosen; naming it by feature tag binds one |
| `verify_t13_hole_count_cardinality` | T13: exactly six Ø8 holes on a Ø100 PCD passes; five and seven fail, every diameter right in all three |
| `verify_t18_dimension_override_and_dimlfac_visible` | T18: a "42" override over 40 and a DIMLFAC 2 over 21 leave the geometry rows passing and are two blocking `dimension_mismatch` findings; honest dimensions are accepted |
| `verify_t25_handle_renumber_rebinds` | T25: a save that gave the bore's handle to the pin is re-bound by geometry (Ø40 passes on the bore); a copied feature tag is `unknown`, never a pick |
| `verify_t36_report_stale_after_file_change` | T36: a line appended to the delivered DXF after delivery: `verify_manifest` names it stale and the acceptance report turns stale and unaccepted |

</details>

The twenty-two checks 1.6 added against v1.5.1 (`miss → pass`) each pin a number computed by hand, not read back from the code that drew it:

<details>
<summary><b>The twenty-two checks 1.6 added</b></summary>

| Track | Check | Verified against |
|---|---|---|
| Understanding | `scale_check_detects_schematic` | the synthetic P&ID reads **schematic** against its layout; a uniform 2× copy of six tags reads **to scale** with factor 2 |
| | `pipe_takeoff_rmst_exact` | all eight routable runs measure exactly the rectilinear MST of their tags |
| | `cable_takeoff_roundup` | 9, 17, 19 and 10 m after the 20 % allowance; the heater's unstated power stays empty |
| | `diff_detects_known_edits` | a move, a text change, an attribute change, an add and a delete, and nothing else |
| | `topology_known_defects` | a 5 mm near miss, one interior crossing, and a clean T that is neither |
| Architecture | `arch_junction_l_t_x` | an L corner mitres at (4100, −100) / (3900, 100), a T stem stops on the near face, an X cuts all four faces; wall areas 1 400 000, 1 490 000 and 2 360 000 mm² |
| | `arch_room_area_net` | a 4 × 5 m room between 200 mm walls is labelled 20.00 m², not the 21.84 m² its axes enclose |
| | `arch_opening_cuts_wall` | both faces are interrupted across a 900 door and two jambs close it |
| | `arch_rooms_detect_foreign` | plain lines on a `WALLS` layer yield 20 and 15 m² at confidence 0.6, and nothing is written |
| Mechanical and sheet | `mech_part_roundtrip` | a part read back out of its own `ACADMCP_MECH` XDATA equals the part drawn |
| | `mech_section_hatch_area` | a 60 × ⌀40 sleeve with a ⌀20 bore cuts 1200 mm², measured out of the drawing by `analysis_measure_entity` |
| | `mech_iso286_on_dimension` | 40 H7 is +0.025 / 0 and survives the layout |
| | `mech_thread_unrepresented_is_caught` | the ISO 6410 focus fires on a thread drawn as a plain circle. A gate that never fires is not a gate |
| | `std_part_iso4014_m12` | s = 18, k = 7.5, read by value |
| | `sheet_frame_iso5457_a3` | the frame runs 20,10 to 410,287 inside the 420 × 297 sheet |
| | `bom_balloon_link` | two identical bolts are one row of quantity 2, and its balloon carries the row's item number |
| Environment | `settings_dimstyle_iso25_values` | the ISO-25 preset lands in the DIMSTYLE table with ISO 129-1's numbers |
| | `settings_layer_state_roundtrip` | save → change → restore puts the layer table back, and the state survives save/reopen |
| | `settings_pdf_mediabox_a3` | the plotted PDF's own `/MediaBox` reads 420 × 297 |
| P&ID | `pid_block_define_attdef_roundtrip`, `pid_tag_parse_fic`, `pid_graph_edge_count` | a block with ATTDEFs defined and read back, an ISA-5.1 tag parsed, a drawn P&ID's edge count |

</details>

### The task matrix — tasks that can fail

An early matrix scored this server 10/10, which carried no information: every task exercised something the server was built around. Since 1.5 a task joins the matrix only if it *can* fail. Three of 1.5's five did while being written.

1.7 adds ten (matrix v8, 31 tasks). Five test what 1.7.0 promises, and five stand on ground a rival server claims:

- `requirements_roundtrip`: a plate verified on the document; then its bore is moved 3 mm inside the saved file, with counts and extents unchanged. Exactly that requirement must fail on the reopened file;
- `cancel_safety`, `qa_large_drawing` (offenders at the 5,001st entity, after the 10,000th and on a paper layout), `export_safety` and `stale_preview`;
- `revision_guarded_edit` (beiming183's revision discipline), `pid_file_roundtrip` (puran-water's P&ID tools), `foreign_edit_verified`, `damaged_dxf_refusal` (refused by name; since 1.8.0, recovered only on request with `drawing_open(recover=True)`) and `dimension_binding`.

1.8.0 adds eighteen (matrix v9, 49 tasks, the runner's default since 1.8.0; `--matrix v8` reproduces the 1.7.0 set):

- `dimension_lie_detect`, `policy_read_only`, `lisp_read_tier` (live engine; `environment_blocked` headlessly), `layer_cleanup_budget` (six misnamed layers and 300 overridden entities clean in at most 3 calls) and `call_count_dimension_part` (a plate dimensioned in at most 2 calls);
- `undo_across_restart` (one `drawing_undo` in a new process puts the line back before the move), `atomic_batch_default` (a batch with a missing handle runs nothing; its run-time twin rolls back) and `history_value_lookup` (the round-2 diameter in at most two calls after a restart);
- `parametric_revise_keeps_handles` (a three-view flange revised in one call, handles kept, signatures equal to a fresh draw; a hand-moved tagged line refused), `pid_routing_obstacles` (an `avoid` sheet with no line through a symbol; a walled-in pump refused `no_route` naming the four vessels), `pid_project_offpage` (three sheet files, four cross-sheet defects each found once), `std_tables_b40` (`not_run` while every B40 table is empty, never a pass) and `dwg_headless_roundtrip` (a DWG opened headlessly through a converter keeps its DXF twin's layers, blocks and entity counts; no converter: `environment_blocked`);
- `replay_across_restart` (a client operation id replayed by a new process with nothing drawn; a server killed inside a `cad_batch` leaves that id `requires_reconcile`), `interrupted_save_recover` (a writer dying half way through a save leaves the file byte for byte as it was; a kill between fsync and replace is finished by the next start), `damaged_dxf_recover` (a truncated DXF with clashing handles opens only with `recover=True`, the source untouched), `bylayer_truth` (22 lines at lineweight Default read back from the saved DXF and named in one grouped issue, all ByLayer after `drawing_refine`) and `space_context` (a circle created on a drawing saved with Layout1 active lands in model space; a trim under a rotated UCS keeps the WCS endpoints). The two kill tasks kill a real `python server.py` with `Popen.kill()`, at a named barrier inside the call where the row says so (`benchmarks/fault_inject.py`), and the run manifest lists every kill point.

Two CI gates hold the call economy: `benchmarks/call_budget.py` runs each scripted job naive (one tool per entity, labelled) and with its least-call oracle - the layer clean-up takes 2 calls (1.7.0: 124, the rival: 11), the plate 2 (1.7.0: 89, the rival: 10) and the value of round 2 after a restart 2 (`drawing_open` + `drawing_history`; 1.7.0 could not answer it) - and `benchmarks/token_suite.py --budget benchmarks/token_budget.json` holds the idle catalog and the task lane to ceilings fixed before W2 began.

The tasks added in 1.5 and 1.6, with what each is verified against:

| Task | Verified against |
|---|---|
| `tool_discovery` | six AutoCAD command names, each ranking #1 |
| `token_budget` | 78,391 → 1,300 wire tokens: the 253-tool `off` catalog against the six tools a default install lists, under a ceiling fixed in advance (2,000). 1.7.0 measured 89,226 → 503 for its 243 tools against three |
| `hatch_islands` | 300 filled with the island, 400 ignoring it |
| `selection_filter` | window 1, crossing 2, bounding box 3, polygon 1 |
| `measure_from_handle` | 139.2699 against the 100.0 a vertex shoelace gives |
| `pid_roundtrip` | the example sheet drawn by `pid_from_spec`, read back by `pid_graph`, which never sees the spec: 5 nodes, 4 edges, 0 dangling, `confidence_min` 1.0, `FIC-101` wired to `FCV-101` |
| `page_setup_truth` | ANSI B, then ISO A3 landscape, applied to Layout1. Each is plotted through `batch_plot` and read back from its PDF's `/MediaBox`: 432 × 279, then 420 × 297 mm, not the setter's return value. A fresh layout is already A3, so the B sheet is what a no-op setter cannot fake |
| `mech_assembly` | a flange-coupling A3 sheet: a hub with a full section, a flange with its bolt-circle end view, two ISO 4014 bolts with ISO 4032 nuts, an ISO 7573 parts list read off the drawing, linked balloons, an ISO 5457 frame and an ISO 7200 title block. Gated on `drawing_critique(focus=None)` returning **zero** issues and a finalize score of at least **90**; a test drops one balloon and watches the gate fail |
| `arch_roundtrip` | a two-room plan (an entrance door, an interior door, two windows, a stair) drawn by `arch_plan_from_spec` and read back by `arch_rooms_detect`, which never sees the spec. Each room must be within 0.1 % of its label and of the net floor computed by hand (4825 × 5750 and 3825 × 5750 mm), and the schedules must list D1, D2, W1, W2. Gated like `mech_assembly`; a test removes one room label and watches the gate fail |
| `takeoff_roundtrip` | the synthetic plant pair: a P&ID stretched 1.3× in one room and rearranged in the other, Russian supply / return words, a return drawn on the supply layer, a reducer, a segment drawn twice. The scale check must call it **schematic** (21.8 % of 55 pairs within ±10 %). `pipe_takeoff` must give every routable run's layout length equal to the rectilinear MST of its tags, sizes and services as drawn, and the run to a tag the layout lacks status C. `cable_takeoff` must give 9 / 17 / 19 / 10 m after the 20 % allowance and leave the heater's power empty. A test removes the wiring callouts and watches the cable half fail by name |
| `understand_foreign` | the pair's layout (inches declared over millimetres, a stray line 5,000 km out, two plan copies) comes back as millimetres with an `INSUNITS` warning, the stray line's handle and two clusters. The P&ID's `P_product piping` / `P_cipsupplyline` / `P_cipreturnline` / `P_ijswater` / `E_power` classify as product / CIP supply / CIP return / ice water / electrical. A test makes the layout declare millimetres and watches the units gate fail |

### Headless performance

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-perf.svg" alt="Performance across four headless workloads" width="800">

The workloads call the same backend methods the MCP tools call, so server-side overhead is included. Numbers move with hardware; the report records the machine fingerprint. **Read the next section before quoting them.** Entity creation is still slower than in v1.4.0.

**1.8.0 against 1.7.0: not slower beyond the noise, with one row to watch.** The same gate, run on 2026-11-05 at `f3523db` against a v1.7.0 worktree: 20 rounds after 5 warm-ups, CPython 3.12.16, every measurement in a fresh state folder of its own ([`perf-v1.7.0-vs-v1.8.0.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/perf-v1.7.0-vs-v1.8.0.json)):

| Workload | v1.7.0 | 1.8.0 | ratio |
|---|---:|---:|---:|
| 2,000 lines, one call each | 272.1 ms | 286.2 ms | 1.02 |
| 10,000 lines: build, DXF export, reopen | 1,682.8 ms | 1,721.3 ms | 1.03 |
| Region query over 10,000 entities | 249.3 ms | 224.5 ms | 0.97 |
| Premium quality pass | 46.9 ms | 41.2 ms | 0.96 |
| 2,000 `entity_create_line` calls through the MCP server | 3,496.3 ms | 3,636.0 ms | 1.06 |

The machine was in use during this run: the A/A spread was 7.1 %, so the bound was 1.14, and every row is inside it. The row to watch is the last one. Its 1.06 is the durable journal (on by default): one `sqlite` commit per mutating call, for the `running` row that lets a restart report an interrupted call as `unknown_commit`, measured at about 50 µs of a 1.7 ms call, with the change summary, the session metrics and the tool policy at about 50 µs more together (`JOURNAL_DURABLE=off` puts the row at 1.03). On a quiet machine the A/A spread was 1.4 % earlier that day, the bound is then the 1.05 floor, and that row misses it by about 1 %; the four others pass either way. It is recorded here, not re-run until it passed. The 1.8.1 item that closes it without changing what a restart reports is an append-only running log: one write per call, the rows written in batches, reconciled at open. The first run of the gate that day measured 1.15 on that row and 1.09 on the roundtrip, because its workers had written into this machine's default state folder, which held a 213 MB journal and 656 checkpoint lineages left by earlier runs; the gate now gives each measurement an empty state folder. The roundtrip's 1.03 is the atomic save (a tmp file fsynced and verified before it replaces the target) and the S0 checkpoint of a reopened file.

**1.7.0 against 1.6.0: not slower beyond the noise.** `benchmarks/perf_compare.py` builds a v1.6.0 worktree and measures both trees in alternating rounds, base, head, head, base, with one interpreter. The A/A calibration measures the base against itself in every round. Values are medians of 20 rounds after 5 warm-ups, and each ratio is the median of the paired per-round ratios ([`perf-v1.6.0-vs-v1.7.0.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/perf-v1.6.0-vs-v1.7.0.json)):

| Workload | v1.6.0 | v1.7.0 | ratio |
|---|---:|---:|---:|
| 2,000 lines, one call each | 492.1 ms | 454.9 ms | 0.95 |
| 10,000 lines: build, DXF export, reopen | 5,139.3 ms | 4,986.8 ms | 0.91 |
| Region query over 10,000 entities | 1,377.6 ms | 1,012.8 ms | 1.02 |
| Premium quality pass | 182.7 ms | 108.0 ms | 0.62 |
| 2,000 `entity_create_line` calls through the MCP server | 14,414.4 ms | 13,412.6 ms | 0.96 |

The gate holds every ratio at or below 1 + max(0.05, 2 × the A/A spread). This run was taken on an idle machine: the A/A spread was 5.5 %, so the bound was 1.11. The region query's 1.02 is inside that noise. Its two medians differ the other way (1,377.6 vs 1,012.8 ms) because the ratio column pairs each round's base and head before taking the median, and the paired figure is the one the gate trusts. These runs use CPython 3.13 and the table below uses 3.11.15, so compare ratios, never milliseconds across tables.

## What this release is bad at

A page that only lists strengths is a page that has not been measured.

**Headless entity creation is slower than v1.4.0, and neither 1.6, 1.7 nor 1.8 won it back.** 1.8.0 is not slower than 1.7.0 beyond the gate's noise, and 1.7.0 not slower than 1.6.0 ([above](#headless-performance)); the loss is 1.6's against v1.4.0, and winning it back is B10, 1.8.1. 1.8.0 adds two measured costs of its own on that path, both named above: the durable journal's commit on every mutating call, and the atomic save's fsync and verification. These are medians of five alternating runs on one machine, with one interpreter (CPython 3.11.15) and one ezdxf, v1.4.0's code against 1.6.0's ([`perf-v1.4.0-vs-v1.6.0.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/published/perf-v1.4.0-vs-v1.6.0.json)):

| Workload | v1.4.0 | v1.6.0 | v1.6.0, `EZDXF_CALL_TIMEOUT=0` |
|---|---:|---:|---:|
| 2,000 lines, one call each | 244.5 ms | 306.1 ms (1.25×) | 284.9 ms (1.17×) |
| 10,000 lines: build, DXF export, reopen | 1,847.9 ms | 2,553.5 ms (1.38×) | 2,216.4 ms (1.20×) |
| Region query over 10,000 entities | 665.2 ms | 712.2 ms (1.07×) | 783.9 ms (1.18×) |
| Premium quality pass | 150.6 ms | 65.3 ms (**2.3× faster**) | 69.9 ms |

The per-call timeout stops one hung call from wedging a server whose document lock is a single `asyncio.Lock`. It explains only part of the slowdown: switched off, creation is still 1.17× and 1.20× slower. The rest is extra work per call that 1.6 has not removed. A cProfile of the event-loop thread counts 491,071 function calls for the 2,000 creates, against v1.4.0's 319,038. Earlier READMEs said that switching the timeout off returned creation to v1.4.0's numbers. Measured this way, it does not. The region-query row, which moves the wrong way, shows this machine's noise floor (about ±10 %). Roadmap criterion 7 is not met and moves to 1.7.1.

**Four mechanical feature tables ship empty.** DIN 471/472 retaining-ring grooves, DIN 509 undercuts, DIN 332 centre holes and ISO 3601-2 O-ring grooves have their drawing code and their provenance tests, but no rows. A groove diameter written from memory is a wrong workshop drawing, while a narrow table is merely narrow. So every size is refused by name until rows verified against the standard's own table are transcribed. Since 1.8.0 a row reaches the tables only through a double entry: typed twice into `engineering/mech/standards/data/<std>.a.json` and `.b.json` by two named people, with the edition, table and page, checked against physical invariants and generated into `_rows.py` by `scripts/build_standards.py` (`--check` is a gate). ISO 3601-2 is now two tables, radial static and axial, so the eight tables are DIN 471, DIN 472, DIN 509-E, DIN 509-F, DIN 332-A, DIN 332-B, ISO 3601-2 and ISO 3601-2-axial. The licensed copies were not at hand for 1.8.0, so every one of them is still empty and deferred to 1.8.1 by name.

**Undo after a restart is as fine-grained as your checkpoints.** Headless, a checkpoint is taken at a save, an open, a new drawing, a rollback batch or a transaction boundary, and at `checkpoint_create` - never per call - so `drawing_undo` after a restart steps back to the last of those, not one call. Label a point you may want back with `checkpoint_create(label)`. Redo across a restart is not kept. On a live seat a checkpoint is a `Document.Export`, taken only at `checkpoint_create`, at each `transaction_begin` and before `entity_delete_many` / `drawing_purge` / `text_find_replace` - not at a save, an open or a new drawing - and it restores only as a new document. Without a state folder (`JOURNAL_DURABLE=off`) there are no checkpoints.

**A part revision refuses what it cannot rebuild.** `mech_part_revise` refuses a part with a detail view or two views sharing a view tag, and over a hand edit of a tagged entity or a part drawn before 1.8.0 unless `overwrite=True` - which replaces the hand edit with the model. Reconciling a revision with a changed spec, and keeping a hand edit, are 1.8.1. Dimension handles change on every revision. A part drawn before 1.8.0 is an `unstamped` info issue of the `model_drift` focus until it is revised.

**The live speed-ups miss three of their four targets.** One display refresh per COM job, bulk tools as one sliced job and owner-block reads for the corner operations are in 1.8.0, and `benchmarks/perf_com.py` measured them against v1.7.0 with targets fixed in advance (AutoCAD 2026, 1.8.0): `entity_batch_create(200)` is 2.9× faster than 200 single creates and `entity_delete_many(200)` 3.8× faster than 200 single deletes, against a target of 5× each; the single `entity_create_circle` median is 8.9 % lower, against 30 %; and the fillet time at 10,000 lines against 5,000, whose target is at most 1.2×, varies between runs on that seat (55× in the committed run, 1.19× in a run an hour earlier). A 1.8.0 fillet is still 4× to 200× faster than 1.7.0's ([`perf_com.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/live/perf_com.json)). The native .NET worker is deferred to 1.9.0 under a rule written before any measurement ([ADR 0001](https://github.com/U-C4N/Autocad-MCP/blob/main/docs/adr/0001-native-worker.md)).

**Takeoffs are estimates, and they say so.**

- A layout length is the rectilinear minimum spanning tree of the tags a run connects, plus a stated vertical allowance, and it is labelled an estimate.
- A P&ID length is used only when `drawing_scale_check` calls the P&ID to scale.
- A power the P&ID does not state stays empty and becomes an open item, and no cable-section table is authored: a section comes only from your `section_rules`.
- Where no callout names a panel, the nearest panel is inferred and flagged.
- Detail copies of equipment are flagged, and excluded only when you ask.

The readers were tuned on the synthetic plant pair and on one real P&ID / layout pair that is not in this repository. Another drafting office's layer and text conventions may need vocabulary.

**Verification measures what it can bind, and says so when it cannot.** A requirement is measured on a feature it can identify: by handle, by the feature tag this server's generators write, by an architectural element, by a geometric predicate or by a DIMENSION's defpoints. On somebody else's drawing that usually means a predicate. Two equal candidates are `ambiguous`, and a spline or a tilted plane is `unsupported`. Neither is a pass, so a report full of `unknown` is a report that needs better selectors, not a clean part.

**A live revision sees the operator's object edits, not everything.** No COM event sink is used (one left the seat refusing `Documents.Add()`); a transient AutoLISP database reactor in the bound document, read through `VL.Application.16`, counts object changes, and a change since the server's previous call is one more `revision`. Measured on AutoCAD 2026: an entity added, moved, re-propertied or deleted, a layer added or changed, ERASE and U each move it by one. A view change and a system variable written with `SetVariable` do not, an edit made while a server call runs is folded into that call, and without Visual LISP the revision counts this server's writes only (`system_status.operator_edits` says which). Two counter reads per live call, 0.6-1.2 ms each (median, two runs). Two servers bound to one document each keep their own counter id, so a read by one never moves the other's revision; a write by one is an outside edit to the other. The preview's entity fingerprints still back every apply.

**The operation journal is durable, and it writes to disk.** Every call's record goes into `<ACADMCP_STATE_DIR>/journal.sqlite3` (default `%LOCALAPPDATA%\autocad-mcp-pro\state`), so a client operation id replays after a restart, and a call that was running when its server died answers `requires_reconcile` (`error_kind: "process_exit"`). By default the arguments are kept up to 4,096 bytes; `JOURNAL_ARGS=digest` keeps only their sha256, and `JOURNAL_DURABLE=off` restores the in-memory journal. A folder inside a repository checkout or beside drawings is refused, and an unusable store leaves the journal in memory with `system_status.state_store.degraded`. No call fails because of its journal, but a restart then forgets.

**A damaged DXF is recovered only when you ask, and only partly.** A file that does not parse is refused by name (`damaged_drawing`, naming `recover=True`) and the open document is kept. `drawing_open(path, recover=True)` reads it with `ezdxf.recover`, which drops what it cannot parse, not always with a fix entry: read `recovered.entities_read` before trusting the result. The recovered drawing will not save over the damaged file without `drawing_save(overwrite_source=True)`. A DWG is not recovered (`recover_unsupported`; use AutoCAD's own RECOVER).

**No Plant 3D.** The roadmap's track D (read-only queries of a Plant 3D project) was dropped: the maintainer does not use Plant 3D, and a reader tested against nobody's real projects would be a guess. Roadmap criterion 5 is withdrawn, not met.

**No REGION or 2D booleans headlessly.** `add_region()` produces a REGION with **zero ACIS bytes**, and the `greiner_hormann` substitute loses **28.2%** of the area on a square with one semicircular edge.

**`system_run_command` / `system_run_lisp` are a guardrail, not a security boundary.** The rejection message says so in those words. A 36-verb denylist refuses the obvious cases (`policy_refused`, with the typed tools for the refused verbs in `alternatives`), but AutoCAD accepts hundreds of commands, and `DANGEROUS_COMMANDS_ENABLED=true` switches the list off entirely. LISP is narrower: under `LISP_POLICY=read` (the default) a real reader runs only the AutoLISP read tier and sends everything else to the same denylist, which also re-scans every read-tier verdict; there is no write tier before 1.8.1 (`LISP_POLICY=denylist` is the 1.7 behaviour, `off` checks nothing). To take the escape hatches away altogether, `TOOL_DENYLIST=system_run_command,system_run_lisp`.

**Path validation is per-tool, and unscoped until you scope it.** With `ALLOWED_PATHS` empty (the default), the only positive bound is a ten-entry system-directory denylist that does not include `C:/Users`, `/home`, `/root` or `/var`. **Set `ALLOWED_PATHS`.**

**Non-AutoCAD ProgIDs are unverified.** `CAD_PROGID` changes which COM application the backend attaches to; nothing beyond the connection has been tested against BricsCAD, ZWCAD or GstarCAD.

### Known limitations of the P&ID track

**The default router is orthogonal and blind; `avoid` sees symbols.** `pid_line_draw` picks `auto` (still the default) from seven orthogonal candidates, by fewest bends then length:

- straight;
- one bend;
- two bends through the midline;
- a stub out of each port joined by three bends.

You can also take `direct`, or pass waypoints. Since 1.8.0, `route="avoid"` (and `pid_from_spec`'s `sheet.routing: "avoid"`) searches an orthogonal route that keeps `clearance` from every symbol that is not one of its ends and from every `keep_out` box; it refuses `no_route` naming the enclosing symbols' tags, or with `on_blocked="cross"` runs through as few blocked grid edges as it can (not always the fewest boxes) and lists them in `obstacle_hits`. Every route *counts* the existing lines it crosses (`crossings`); `avoid` adds a cost for each, but a crossing is allowed, never refused. A dense sheet can exhaust the 50,000-node search (`routing_budget_exceeded`; `pid_line_draw` does not expose the budget, so pass waypoints), a line-number label is placed after routing and is only reported when it overlaps a symbol (`label_hits`), and a per-line `avoid` inside `pid_from_spec` is refused.

**A foreign drawing is read at the confidence it earns, and no higher.** A block this server did not place classifies at 0.6, with inferred ports, by its `TAG`-style attribute or a keyword in its name. It classifies at 0.3 when a line merely touches it, and a 0.3 guess can neither raise nor suppress a critique finding.

On the live engine, an INSERT turned off a right angle has members ActiveX cannot measure, so it reports an *approximate* box. `pid_graph` takes the tighter wall of that box and the attribute-inclusive box on every side. Even so, a line ending on such a body's true edge can still be reported `dangling`, with the nearest port as the hint. Read `stats.confidence_min` first.

**Projects are read, not written.** `pid_graph` links off-page connectors within one drawing; `pid_project` (1.8.0) matches them across up to 50 sheet files, finds cross-sheet duplicate tags and line-number breaks, and never modifies a sheet. A project-wide tag sequence, and refusing a tag another sheet already holds, are 1.8.1. LINK ids are compared case-sensitively and tags case-folded. A `.dwg` sheet is read through the live backend (a headless `pid_project` refuses it; a headless `drawing_open` converts one only when ODA or `accoreconsole` is installed). There is no DEXPI/Proteus export and there are no ISA-5.2 binary-logic symbols in this release. Both cuts are recorded in the spec so nobody re-derives them.

### Known limitations of the settings track

**Layer states are ours.** `layer_state_save` writes a portable snapshot into an `ACADMCP_LAYERSTATES` XRECORD. It survives save/reopen on both engines and travels with the DWG/DXF. It is **not** an AutoCAD `LAYERSTATE` and does not appear in the Layer States Manager, which is why `system_capabilities` reports `layer_states` as `mode: "xrecord"`.

**Six of the 22 environment tools need a live seat.** Preferences, launching, the operator prompts and the five summary fields of document properties are refused headlessly with `preferences` / `dwgprops` / `live_application` / `interactive_prompt`. Each is a refusal, not a stub.

A `.dwt` template is the same case (`dwt_write`): a `.dwt` is a DWG container, so headlessly the template is saved as DXF. `mleaderstyle_create` is *not* such a case. ActiveX has no MLeaderStyle collection, but the `ACAD_MLEADERSTYLE` dictionary holds full `IAcadMLeaderStyle` objects, so both engines create leader styles.

**A dimension is redrawn on a style change, a scale and a point edit only.** Since 1.8.0, `dimstyle_modify` redraws every dimension using the style on both engines (`rerendered`; live through `Update()` and a read-back of the `*D` text), and `entity_scale` and `entity_edit_geometry` redraw the DIMENSION they change. A move, rotate, mirror, copy or array does not redraw it yet (1.8.1), and neither does a DIMENSION inside a block definition; `dimension_truth` reports a block that shows another text than its style formats as `dim_render_stale`. Headless, ezdxf 1.4.4 draws a two-line angular dimension's reflex angle, which that focus reports as not repairable here: redraw it in AutoCAD. The live redraw has not yet been run on a seat.

### Known limitations of the architecture track

**Nominal, not standard.** The furniture and sanitary blocks carry nominal catalogue sizes and say so. The stair tool reports the Blondel relation (2R + G within 600–650 mm) as a design rule, never as a building code. The `arch` layer colours are a repository convention: ISO 13567 fixes the names, not the colours. Labels speak English or Turkish; any other `lang` is refused with the list.

## Configuration

Nothing loads a `.env` file — export these, or set them in your MCP client's `env` block.

<details>
<summary><b>All 46 environment variables</b></summary>

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCAD_MCP_BACKEND` | `auto` | `auto`, `com`, or `ezdxf` |
| `CAD_PROGID` | `AutoCAD.Application` | COM ProgID the live backend attaches to |
| `TOOL_PROFILE` | `full` | `lean` (67 curated tools, 57 with `TOOL_PACKS=core`) or `full` |
| `TOOL_PACKS` | `all` | Vertical packs to advertise: `core,pid,settings,mech,arch,plant` (`core` always on) |
| `DISCOVERY_MODE` | `search` | `search` (the default since 1.8.0) lists `search_tools` + `call_tool` + `describe_tool` and the `DISCOVERY_PINNED` tools, and every tool stays callable by name; `off` advertises the full catalog (the 1.7 default). An unknown value falls back to `off` with a warning |
| `DISCOVERY_PINNED` | `cad_batch,drawing_info,entity_list` | Comma-separated tools search mode lists directly, as shortened cards (full input schema; `describe_tool` returns the whole description). Empty pins nothing; a pin the profile or packs hide is not listed, an unregistered name is warned about at startup |
| `ENABLE_3D` | `false` | Expose the opt-in `solid_*` tools (COM) |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ALLOWED_PATHS` | *(empty)* | Comma-separated absolute paths the server may access |
| `MAX_UNDO_STACK` | `5` | Maximum retained undo snapshots |
| `EZDXF_UNDO_DEPTH` | `0` | Headless per-call undo steps (a full DXF snapshot per mutating call), used before the durable checkpoints; `0` leaves undo to the checkpoints |
| `MAX_DXF_BYTES` | `536870912` | Reject larger DXF input (512 MB); `0` disables |
| `MAX_QA_ENTITIES` | `2000000` | Refuse a QA read (critique, validator, finalize, deliver) of a larger drawing with `scan_incomplete`; `0` disables, a negative value is refused at startup |
| `MAX_LIST_LIMIT` | `5000` | Bound list/selection response sizes |
| `MAX_SPEC_ITEMS` | `2000` | Items per list of a `pid_from_spec` / `arch_plan_from_spec` document; `0` disables |
| `MAX_VESSEL_TRAYS` | `500` | Trays a parametric column may carry; `0` disables |
| `COM_CALL_TIMEOUT` | `60` | Per-call live AutoCAD timeout (s); `0` disables |
| `COMMAND_DEADLINE_S` | `8` | How long a command sent to AutoCAD's command line (`system_run_command`, live undo / redo, corner operations, the transaction mark) may stay active; past it AutoCAD's `LASTPROMPT` is read, ESC is sent three times and the call fails `command_incomplete` |
| `COM_REFRESH_UPDATE_MAX` | `1` | A live job that asked for at most this many display refreshes, each naming its entity, calls `Update()` on the last one; a larger job does one `Regen`. `0` makes every job Regen |
| `COM_REFRESH_MODE` | `auto` | `regen` forces one `Regen` per live job (for a graphics setup where `Update()` does not paint). An unknown value falls back to `auto` with a warning |
| `ACCORECONSOLE_PATH` | *(unset)* | The `accoreconsole.exe` the headless `drawing_open` converts a `.dwg` with, after the ODA File Converter. Unset uses AutoCAD 2026's installed console when it exists; `off` disables the route |
| `ACCORECONSOLE_TIMEOUT_S` | `60` | One DWG -> DXF conversion's deadline (kept inside `EZDXF_CALL_TIMEOUT`); an overrun kills the console's process tree. Must be a number above 0 |
| `EZDXF_CALL_TIMEOUT` | `120` | Per-call headless timeout (s). A read that overruns is never quarantined: until it ends, other calls on its document are refused by name (`kind: "busy"`) while `drawing_open` / `drawing_new` / `drawing_close` / `document_activate` / `document_close` and `document_list` go through without waiting for it — no restart is needed. They do not wait on its lock, but a read that computes holds Python's GIL, so the server answers slowly until it ends: an 8 s CPU-bound read at `1` s gave its own error at 5.1–5.5 s, a busy refusal in 1.2–1.8 s and `drawing_new` in 0.6–1.3 s (a read that waits on I/O answers in milliseconds). `0` disables the timeout: nothing is quarantined or detached, so a call that never ends keeps the document — every later call, the exits included, queues behind it, and only its end or a restart frees it |
| `SHUTDOWN_GRACE` | `5` | Seconds shutdown waits for a worker still inside a call before exiting without it; `0` does not wait |
| `ACADMCP_STATE_DIR` | `%LOCALAPPDATA%\autocad-mcp-pro\state` (`$XDG_STATE_HOME/autocad-mcp-pro` elsewhere) | The operation journal, pending-save markers, file lineage, durable checkpoints (`docs/<lineage_id>/cp/`) and the `accoreconsole` staging folder. A folder inside a repository checkout or one holding drawings is refused at startup and the journal runs in memory (`system_status.state_store.degraded`) |
| `JOURNAL_DURABLE` | `on` | `off` keeps the operation journal in memory only (the 1.7 behaviour: a restart forgets every operation id) and takes no checkpoints: the checkpoint tools are refused `checkpoints`, and the headless `drawing_undo` needs `EZDXF_UNDO_DEPTH` again |
| `JOURNAL_ARGS` | `bounded` | What a journal row keeps of the arguments: `bounded` (canonical JSON up to 4,096 bytes), `digest` (sha256 only; answers kept as a hash too) or `off` |
| `JOURNAL_SYNC` | `normal` | sqlite `synchronous`: `normal` loses nothing on a process kill; `full` also covers an OS crash or power loss, at about 30× the cost per commit |
| `JOURNAL_RETAIN_DAYS` | `30` | Journal rows older than this are dropped when the store opens |
| `JOURNAL_RESULT_MAX_KB` | `64` | A client id's answer above this keeps only its sha256 and replays as `result_truncated: true` |
| `CHECKPOINT_KEEP` | `20` | Checkpoints kept per drawing lineage besides its first (S0); older ones are evicted (listed with `evicted: true`, restoring one is refused `checkpoint_evicted`) |
| `CHECKPOINT_BUDGET_MB` | `512` | The checkpoint disk budget across lineages: the least recently written lineage, oldest checkpoint first, is evicted, never the one just written (so the disk stays at or below the budget plus one checkpoint) |
| `CHECKPOINT_MAX_DOC_MB` | `32` | The most checkpoint bytes one lineage keeps |
| `SAVE_BACKUP` | *(off)* | `1` copies the previous file to `<name>.bak` before a headless save replaces it |
| `READ_ONLY` | `false` | `true` runs only `read` tools and writes no file; anything else is refused `policy_refused` before it runs |
| `TOOL_ALLOWLIST` | *(empty)* | Comma-separated tool names or globs (`entity_*`); only these are advertised and callable. An entry naming no tool is warned about at startup (`system_about.policy.unmatched`) |
| `TOOL_DENYLIST` | *(empty)* | Comma-separated tool names or globs that are neither advertised nor callable |
| `MAX_TOOL_COST` | `escape` | The highest tool cost a call may carry: `read`, `safe`, `mutate`, `destructive` or `escape`. A file write is `mutate` whatever its tool is labelled |
| `DANGEROUS_COMMANDS_ENABLED` | `false` | Allow blocked commands/LISP; reported as unsafe mode |
| `LISP_POLICY` | `read` | How `system_run_lisp` checks an expression (live only): `read` runs the AutoLISP read tier and sends the rest to the 1.7 denylist, `denylist` is the 1.7 behaviour, `off` checks nothing. An unknown value falls back to `read` with a warning |
| `ALLOW_REMOTE_HTTP` | `false` | Permit a non-loopback HTTP bind (which also needs a token and `ALLOWED_PATHS`) |
| `MCP_AUTH_TOKEN` | *(empty)* | Bearer token required for remote HTTP; scopes `mcp:read` and `mcp:write` |
| `MCP_AUTH_TOKEN_READONLY` | *(empty)* | A second bearer token with only `mcp:read`: its requests run under the `read` ceiling and see only read tools. Alone it also enables auth |
| `HTTP_ALLOWED_ORIGINS` | *(empty)* | Origins an HTTP request may carry; any other Origin is refused 403. A browser-based MCP inspector needs its own origin listed (e.g. `http://localhost:6274`); `*` allows all |
| `HTTP_ALLOWED_HOSTS` | *(empty)* | Host headers accepted besides the bound host:port and the loopback names; any other is refused 421. `*` allows all |
| `MAX_HTTP_BODY_BYTES` | `8388608` | An HTTP request body over this (8 MiB) is refused 413, counted on chunked bodies too; `0` disables |

</details>

## Architecture

```mermaid
flowchart LR
  A[MCP host / AI agent] --> B[FastMCP 3 server]
  B --> C[Error · audit · timing · logging · capability-refusal middleware]
  C --> D[Typed contract · 23 modules · 45 capability keys]
  D --> E[COM backend<br/>single-STA thread]
  D --> F[ezdxf backend<br/>asyncio.to_thread]
  E --> G[Live AutoCAD]
  F --> H[Headless DXF]
  E --> I[Engineering · critique · scoring · delivery]
  F --> I
```

<details>
<summary><b>The drawing at the top of this page, as tool calls</b></summary>

The sequence in [`scripts/render_readme_showcase.py`](https://github.com/U-C4N/Autocad-MCP/blob/main/scripts/render_readme_showcase.py), which produced the hero sheet:

```text
drawing_new()                                    ISO linetypes + layers bootstrapped
drawing_apply_iso_layers("mech")                 ISO 128 lineweights per layer
drawing_settings({units: "mm", linear_precision: 2})

gear_draw_spur_front_view(                       involute flanks, not a decorated circle
    module=6, teeth=24, center=[135, 172],
    bore_diameter=40, keyway_width=12, keyway_depth=3.3)
gear_draw_section_aa(x_offset=300, face_width=46)

dimension_linear(...)                            ISO 129
dimension_diameter(..., fit="H7")                deviations from authored ISO 286 tables
titleblock_apply_iso_a3(title="SPUR GEAR m6 z24", material="C45E", ...)

drawing_critique(focus=None)                     -> []  must be empty before finalize
view_screenshot()                                -> PNG
```

The full production loop adds `drawing_preflight` and `drawing_plan` at the front, `drawing_refine` after the critique, and `layout_create` + `viewport_create` + `drawing_deliver` at the end.

</details>

## Development

```bash
uv sync --locked --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

`uv.lock` pins the whole transitive graph, so this reproduces CI exactly. CI runs Linux (3.11 / 3.12) and Windows (mocked COM), plus package, Docker and MCP-registry jobs. It also runs `claude plugin validate --strict` on the marketplace and the plugin, the generated-file gates (`build_plugin.py --check`, `generate_tool_inventory.py --check`, `check_doc_numbers.py`), the `token-budget` job (`python -m benchmarks.token_suite --lanes idle,task --budget benchmarks/token_budget.json` and `python -m benchmarks.call_budget`: a `ci`-gated ceiling breached, an idle row grown more than 5 % over the published report or a scripted job not done fails the PR) and a contents check of the built wheel and sdist: the wheel holds the server and nothing else, and the sdist holds only what git tracks. The release gate runs the same three test lanes, plus a headless lane that runs the correctness suite against the previous tag with `--fail-on-regression`, before anything reaches PyPI. Releases are tag-driven: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The live COM paths are checked on a running AutoCAD, never in CI: `python scripts/smoke_verify_com.py --json out/verify.json` (and the twelve other `scripts/smoke_*_com.py`) attaches to the open seat, works only in documents it creates, and closes them. `python scripts/live/run_all.py` runs all fourteen, each in its own process, and writes one matrix per release. For 1.8.0 every one passed in one run of one committed tree, with `scripts/smoke_accore.py` (the headless DWG read through `accoreconsole`) among them (AutoCAD 2026, 1.8.0): [`matrix.json`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/results/live/1.8.0/matrix.json). That run passed the five smokes written for 1.8.0, `smoke_space_com.py`, `smoke_edit_com.py`, `smoke_render_com.py`, `smoke_lisp_com.py` (the AutoLISP read tier) and `smoke_durability_com.py` (checkpoints, the refused in-place restore, a `new_document` restore), and the stages 1.8.0 added to older ones: `set edits`, `dimension truth` and `dimension auto` in `smoke_settings_com.py`, the revise stage in `smoke_mech_com.py`, and the `avoid` and `project` stages in `smoke_pid_com.py` (AutoCAD 2026, 1.8.0). The B40 stage of `smoke_mech_com.py` ran in the same run (AutoCAD 2026, 1.8.0) and skipped all eight feature tables, which ship with no rows.

## Roadmap

**1.7.1**

- **Speed.** Win back the headless creation throughput that 1.6 lost against v1.4.0 (criterion 7). Arm the per-call timeout only where a call can block, and remove the per-call work that came with it.
- **The XDATA rework**: chunking, case and budget.

**1.8**

- *In 1.8.0 (first phase, see the [changelog](https://github.com/U-C4N/Autocad-MCP/blob/main/CHANGELOG.md)):* the durable operation journal with idempotency across restarts, atomic saves, `drawing_open(recover=True)` (`ezdxf.recover`, first planned for 1.7.1), one typed error envelope, the `property_override` focus, in-place polyline and label edits, region screenshots, and the live engine's target space. *Second phase:* set-based edits (`entity_modify_where`, `layer_rename`, `layer_merge`), `dimension_auto` for a whole part, the QA coverage ledger and compact QA answers, the `dimension_truth` focus with dimensions redrawn on a style change, the tool and write-path policy and the HTTP guard, the AutoLISP read tier, session metrics, the token and call budgets in CI, durable checkpoints with undo after a restart, the atomic `cad_batch` default, `drawing_history`, and search mode by default. *Third phase:* `mech_part_revise` (parametric edit and rebuild of a drawn part, with its model history), obstacle-aware P&ID routing, `pid_project` across sheet files, a headless DWG read through `accoreconsole`, one display refresh per live job and bulk tools as one job, the B40 double-entry pipeline, and ADR 0001.
- **1.8.1:** the transcribed rows for DIN 471/472, DIN 509, DIN 332 and ISO 3601-2 (all eight tables deferred, each waiting on its licensed copy), the LISP write tier, batch lease and `confirm_large`, `cad_batch` step coalescing, the `pid_project` write side, `mech_part_revise`'s reconciliation and keep-the-hand-edit modes, DWG export and AUDIT through `accoreconsole`.

**1.9**: IEC 60617 electrical schematics, reusing the P&ID track's symbol, port and graph machinery. A native .NET worker only if [ADR 0001](https://github.com/U-C4N/Autocad-MCP/blob/main/docs/adr/0001-native-worker.md)'s rule fires on live measurements.

Architecture decisions live in [`docs/adr/`](https://github.com/U-C4N/Autocad-MCP/tree/main/docs/adr): ADR 0001 records why 1.8.0 ships no native .NET worker and the rule that decides it for 1.9.0.

Features ship when their contracts and limitations are testable — not when they make a longer checklist.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=U-C4N/U-Pool,U-C4N/Autocad-MCP&type=Date&legend=top-left)](https://www.star-history.com/#U-C4N/U-Pool&U-C4N/Autocad-MCP&Date&legend=top-left)

## Author

**Umutcan Edizsalan** · Mechanical engineering work at **Anka-Makine** · GitHub [@U-C4N](https://github.com/U-C4N)

Built from production drawing work, then made model-agnostic through MCP.

## License

[MIT](https://github.com/U-C4N/Autocad-MCP/blob/main/LICENSE)

<!-- MCP registry ownership marker; must equal the "name" in server.json. -->
mcp-name: io.github.u-c4n/autocad-mcp
