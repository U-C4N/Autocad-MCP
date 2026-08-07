<div align="center">

# AutoCAD MCP Pro

**Production-grade AutoCAD automation for AI agents.**
Live through COM on Windows, or headless through ezdxf anywhere — one typed contract, two engines.

[![CI](https://github.com/U-C4N/Autocad-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/U-C4N/Autocad-MCP/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/autocad-mcp-pro?color=1f6feb&label=PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/autocad-mcp-pro/)
[![Downloads](https://img.shields.io/pypi/dm/autocad-mcp-pro?color=8957e5&label=downloads)](https://pypi.org/project/autocad-mcp-pro/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-1f6feb?logo=python&logoColor=white)](https://github.com/U-C4N/Autocad-MCP/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-3fb950)](https://github.com/U-C4N/Autocad-MCP/blob/main/LICENSE)

[Install](#install) · [Tools](#what-you-get) · [Engines](#the-two-engines) · [Evidence](#evidence) · [Limits](#what-this-release-is-bad-at) · [Config](#configuration) · [Changelog](https://github.com/U-C4N/Autocad-MCP/blob/main/CHANGELOG.md)

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-showcase.png" alt="ISO A3 sheet: a module-6, 24-tooth spur gear with a keyed bore, its section A-A, ISO 129 dimensions with an H7 bore fit, and an ISO 7200 title block" width="800">

<sub><b>Not a mockup.</b> Every line on this sheet was drawn by the tools this server exposes — ISO layers, involute gear geometry, DIN 6885 keyway, section A-A, ISO 129 dimensions, an ISO 286 <code>H7</code> bore fit, ISO 7200 title block — then rendered headlessly by <code>view_screenshot</code>. <code>drawing_critique</code> returns <b>0 issues</b> on it. Rebuild it with <code>python scripts/render_readme_showcase.py</code>.</sub>

</div>

> **v1.5 release snapshot:** 154 tools · 6 resources · 5 prompt templates · 1369 collected tests.
> 154 is the **registered** count; a default install advertises 149 over `tools/list`,
> because `ENABLE_3D` is unset. `system_about` is the runtime authority.

## Why this exists

**A big MCP server is expensive to be connected to.** The full catalog costs a client **40,305 tokens** before it has asked for anything. Discovery mode replaces it with two tools and costs **356**.

**A drafter searches for `FILLET`, not `entity_fillet`.** Those command names appeared in no tool name or description — `df = 0` against a stock index, not badly ranked but *absent*. The fix was data: an authored corpus of **158 AutoCAD command names and 672 synonym phrases** covering all 154 tools. A test refuses to let a tool exist without one.

| Advertised surface | Tools seen | Idle cost |
|---|---:|---:|
| `TOOL_PROFILE=full` (default) | 149 | 40,305 tokens |
| `TOOL_PROFILE=lean` | 47 | 12,131 tokens |
| `DISCOVERY_MODE=search` | 2 | **356 tokens** |

> [!NOTE]
> Offline ratio estimates, not tokenizer counts, and the *uncached* ratio — prompt caching amortises the idle term. `benchmarks/token_suite.py --tokenizer anthropic` counts for real.

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
| `[full]` | everything above | Development and CI |

> [!NOTE]
> The bare install **cannot draw pixels** — `ezdxf.addons.drawing` imports Pillow unconditionally, so every render path needs it. Add `[pdf]` for images on Linux or macOS.

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
        "DISCOVERY_MODE": "off"
      }
    }
  }
}
```

Claude Desktop, Cursor, or any stdio MCP host. For HTTP: `autocad-mcp --transport http --port 8000` — loopback only unless remote HTTP is explicitly enabled **and** a bearer token is set.

</details>

## What you get

| Area | What it does |
|---|---|
| Drawing lifecycle | create, open, save, export DXF/PDF, audit *(repairs)*, purge, undo/redo |
| Geometry | lines, arcs, polylines, splines, hatches, trim/extend/fillet/chamfer, handle-preserving edits |
| Annotation | ISO 129 toleranced dimensions, ISO 286 fits (`fit="H7"`), TABLE, MLEADER, GD&T frames and datums (ISO 1101) |
| Engineering generators | involute gears (front + section A-A), DIN 6885 keyed bores, ISO A3 title block |
| Discovery | `search_tools` ranked over an AutoCAD command and synonym corpus — `FILLET`, `BPOLY`, `QSELECT`, `WBLOCK`, `OVERKILL`, `CHSPACE` each rank **#1** of the 149-tool advertised catalog |
| Batching | `cad_batch` runs a step list in one round trip; `fields=` projects 11 result-heavy tools |
| Paper space | tab lifecycle, viewports, `entity_change_space` (CHSPACE), `drawing_export_pdf(layout=…)` |
| Selection | window vs crossing stated back to the caller; a polygon tested against its own shape, not its bounding box |
| Boundaries | `boundary_trace` (BOUNDARY/BPOLY) chains loose edges into one closed polyline, arcs kept as bulges *(headless)* |
| Measurement | `analysis_measure_entity` measures what is *in* the drawing, by handle |
| Hatch depth | gradients, in-place edits, typed edge boundaries *(headless)*, island styles |
| Annotation objects | WIPEOUT, REVCLOUD, MTEXT background masks *(headless)*, text find/replace |
| 3D solids | `solid_box/cylinder/extrude/revolve/boolean` on live AutoCAD (`ENABLE_3D=true`) |
| Quality loop | `drawing_preflight` → `drawing_plan` → `drawing_critique` → `drawing_refine` → `drawing_finalize` (0–100 score) |
| Delivery | `drawing_deliver`: DXF/PDF/PNG + SHA-256 manifest + reopen-parity checks |

<sub>154 tools in 19 groups. Plus 6 resources that cost nothing in the tool budget (<code>autocad://drawing/info</code>, <code>layers</code>, <code>blocks</code>, <code>entities/stats</code>, <code>entities/{layer_name}</code>, <code>system/status</code>) and 5 prompt templates.</sub>

**Two rules worth knowing.** Every coordinate in and out of a tool is WCS on both engines — the one exception is TEXT `rotation`, which stays in the entity frame because a mirrored TEXT is mirror-imaged and no scalar angle expresses that. And never read vertices back and shoelace them: that loses **28.2%** of the area on a semicircular edge, silently. `analysis_measure_entity(handle)` reads the real geometry and states its own accuracy.

## The two engines

One contract across 19 modules in `backends/contracts/`. `@capability(key, reason=…)` supplies a default that *refuses*, and a test holds both backends' capability key sets equal. There are **27 capability keys**; read `system_capabilities` at runtime rather than trusting the table — five of them depend on how the machine is set up.

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
| REGION / 3DSOLID area | ✅ | — *ACIS is opaque to ezdxf* |
| 3D solids | ✅ *with `ENABLE_3D=true`* | — |
| DWG write | ✅ | — |
| WIPEOUT · MTEXT background colour | — *verified absent, AutoCAD 2026* | ✅ |
| REVCLOUD · BPOLY · typed hatch edges | — *no ActiveX member* | ✅ |
| CHSPACE | — *unverified on a live seat* | ✅ ᶜ |
| Undo history | ✅ | opt-in — `EZDXF_UNDO_DEPTH` |
| TABLE and MLEADER | Native | Portable composite |
| Screenshots and PDF | Window capture | Matplotlib ᵐ |

<sub>ᵐ Needs matplotlib (<code>[pdf]</code>/<code>[full]</code>); without it <code>png</code>, <code>pdf</code>, <code>viewport_render</code> and <code>handle_overlay</code> report unsupported headlessly.<br>
ᶜ Headless CHSPACE carries four named restrictions in its capability reason: top-view untwisted viewports only, dimensions refused unless frozen, ACIS/proxy/table refused, viewport clipping reported rather than applied.</sub>

> [!IMPORTANT]
> Every COM path added in v1.5 was executed against a **live AutoCAD 2026**. That run found four defects reading could not — the largest being that `AcadPViewport` has no `ViewCenter` member, so the line setting it had been silently swallowed by a bare `except` since v1.4.

## Evidence

Self-measurement, produced by scripts in [`benchmarks/`](https://github.com/U-C4N/Autocad-MCP/blob/main/benchmarks/).

### Correctness — every release re-proves itself

26 deterministic headless checks against the previous tag and the current tree, each in its own subprocess so a hard crash counts as a miss rather than killing the run.

| Version | Checks passing | Pass rate | Fixed | Regressed |
|---|---:|---:|---:|---:|
| v1.5.0 *(baseline)* | 24 / 26 | 92.3 % | — | — |
| **v1.5.1** *(this release)* | **26 / 26** | **100 %** | 2 | **0** |

Against the older `v1.4.0` baseline the same suite reports **21 / 26 → 26 / 26, five fixed, zero regressed**. Three of the five are new capability (`miss → pass`); two are repaired defects (`fail → pass`) — the diameter and radius callouts, which measured the leader as geometry and dimensioned a 40 mm bore as 60 at default settings.

### The task matrix — five tasks that can fail

An earlier matrix scored this server 10/10, which carried no information: every task in it exercised something the server was built around. Five were added because they *can* fail, and three did while being written.

| Task | Verified against |
|---|---|
| `tool_discovery` | six AutoCAD command names, each ranking #1 |
| `token_budget` | 40,305 → 356 tokens, against a ceiling fixed in advance |
| `hatch_islands` | 300 filled with the island, 400 ignoring it |
| `selection_filter` | window 1, crossing 2, bounding box 3, polygon 1 |
| `measure_from_handle` | 139.2699 against the 100.0 a vertex shoelace gives |

### Headless performance

<img src="https://raw.githubusercontent.com/U-C4N/Autocad-MCP/main/docs/assets/autocad-mcp-perf.svg" alt="Performance across four headless workloads" width="800">

Workloads call the same backend methods the MCP tools call, so server-side overhead is included. Numbers move with hardware; the report records the machine fingerprint. **Read the next section before quoting them** — this release is slower than v1.4.0 at creating entities, on purpose.

## What this release is bad at

A page that only lists strengths is a page that has not been measured.

**Entity creation is slower than v1.4.0.** Median of three runs on one machine and interpreter: 2,000 lines **1.4× slower**, the 10,000-line roundtrip **2.3× slower**. Attributed — setting `EZDXF_CALL_TIMEOUT=0` returns creation to v1.4.0's numbers. The cost is the per-call `asyncio.wait_for` wrapped around every headless call so one hung call can no longer wedge a server whose document lock is a single `asyncio.Lock`. Deliberate trade, documented knob, on the 1.6 roadmap. The premium quality pass went the other way — **3.7× faster**.

**Wave A shipped 13 tools of a planned 39.** Every cut is backed by a measurement. The REGION and 2D-boolean family went because `add_region()` produces a REGION with **zero ACIS bytes**, and the `greiner_hormann` substitute loses **28.2%** of the area on a square with one semicircular edge.

**`system_run_command` / `system_run_lisp` are a guardrail, not a security boundary** — the rejection message says so in those words. A 36-verb denylist refuses the obvious cases, but AutoCAD accepts hundreds of commands and `DANGEROUS_COMMANDS_ENABLED=true` switches it off entirely.

**Path validation is per-tool, and unscoped until you scope it.** With `ALLOWED_PATHS` empty — the default — the only positive bound is a ten-entry system-directory denylist that does not include `C:/Users`, `/home`, `/root` or `/var`. **Set `ALLOWED_PATHS`.**

**Non-AutoCAD ProgIDs are unverified.** `CAD_PROGID` changes which COM application the backend attaches to; nothing beyond the connection has been tested against BricsCAD, ZWCAD or GstarCAD.

## Configuration

Nothing loads a `.env` file — export these, or set them in your MCP client's `env` block.

<details>
<summary><b>All 16 environment variables</b></summary>

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCAD_MCP_BACKEND` | `auto` | `auto`, `com`, or `ezdxf` |
| `CAD_PROGID` | `AutoCAD.Application` | COM ProgID the live backend attaches to |
| `TOOL_PROFILE` | `full` | `lean` (47 curated tools) or `full` |
| `DISCOVERY_MODE` | `off` | `search` replaces the catalog with `search_tools` + `call_tool` |
| `ENABLE_3D` | `false` | Expose the opt-in `solid_*` tools (COM) |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ALLOWED_PATHS` | *(empty)* | Comma-separated absolute paths the server may access |
| `MAX_UNDO_STACK` | `5` | Maximum retained undo snapshots |
| `EZDXF_UNDO_DEPTH` | `0` | Headless undo history depth; `0` disables it |
| `MAX_DXF_BYTES` | `52428800` | Reject larger DXF input; `0` disables |
| `MAX_LIST_LIMIT` | `5000` | Bound list/selection response sizes |
| `COM_CALL_TIMEOUT` | `60` | Per-call live AutoCAD timeout (s); `0` disables |
| `EZDXF_CALL_TIMEOUT` | `120` | Per-call headless timeout (s); `0` disables |
| `DANGEROUS_COMMANDS_ENABLED` | `false` | Allow blocked commands/LISP; reported as unsafe mode |
| `ALLOW_REMOTE_HTTP` | `false` | Permit a non-loopback HTTP bind |
| `MCP_AUTH_TOKEN` | *(empty)* | Bearer token required for remote HTTP |

</details>

## Architecture

```mermaid
flowchart LR
  A[MCP host / AI agent] --> B[FastMCP 3 server]
  B --> C[Error · audit · timing · logging · capability-refusal middleware]
  C --> D[Typed contract · 19 modules · 27 capabilities]
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

`uv.lock` pins the whole transitive graph, so this reproduces CI exactly. CI runs Linux (3.11 / 3.12), Windows (mocked-COM), plus package, Docker and MCP-registry jobs; the release gate runs the same three test lanes before anything reaches PyPI. Releases are tag-driven: `git tag vX.Y.Z && git push origin vX.Y.Z`.

## Roadmap (1.6)

Give back the creation throughput the per-call timeout costs — arm it only for calls that can actually block. ISO 286 interference **shafts** r/s/t/u. HATCH boundary *geometry* in `entity_get` (1.5 measures a hatch's filled area but does not hand back its loops). REGION/3DSOLID area headlessly, which needs a modelling kernel ezdxf does not have. An *allowlist* of permitted AutoLISP heads, replacing the denylist — enumerating dangerous symbols does not terminate. `ezdxf.recover` as a fallback on `drawing_open`.

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
