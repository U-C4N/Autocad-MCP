# AutoCAD MCP Pro

**Production-grade AutoCAD automation for the Model Context Protocol.**
Dual-engine. Battle-tested. Model-agnostic.

v1.1.0 · 110 tools · 5 resources · 5 prompt templates · COM + ezdxf backends · Python 3.11+ · MIT

---

## From the Author

I built **AutoCAD MCP Pro** because I needed it.

My day job at **Anka-Makine** revolves around intensive AutoCAD work — production drawings, parametric assemblies, strict layer standards across dozens of templates, dimensioning and block work under tight schedules, repeated audits, exports, and screenshots for design reviews. The repetitive parts of that workflow add up fast.

After months of leaning on this server inside my own daily workflow — and watching it shave hours off my week without a single misstep on critical drawings — I decided it deserved a public release. It works. It is fast. It stays out of the way.

The model on the other end of the wire does not matter. The Model Context Protocol is the contract; AutoCAD MCP Pro is one well-typed implementation of that contract. Any MCP-aware client — and the LLM behind it — sees the same 110 tools.

I will keep this repository actively maintained as my own use of it evolves, and **a public benchmark suite is on the way** — I want to give you numbers, not adjectives.

— *Umutcan Edizsalan · Anka-Makine*

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Highlights](#highlights)
- [AI Client Compatibility](#ai-client-compatibility)
- [Quickstart](#quickstart)
- [Installation](#installation)
- [MCP Client Configuration](#mcp-client-configuration)
- [Backend Selection](#backend-selection)
- [Configuration Reference](#configuration-reference)
- [Tool Catalog](#tool-catalog)
- [Architecture](#architecture)
- [Security Model](#security-model)
- [Benchmarks](#benchmarks)
- [Development](#development)
- [Roadmap](#roadmap)
- [Author](#author)
- [License](#license)

---

## Why This Exists

Most CAD-automation tooling falls into two camps: heavyweight enterprise plug-ins that lock you into a single vendor, or one-off Python scripts that solve a single drawing and rot a week later. Neither plays well with the modern AI workflow, where the *agent* — not the engineer — is the one driving the tool.

AutoCAD MCP Pro takes a different stance:

1. **One protocol, two engines.** A live **COM** backend talks to a running AutoCAD instance for in-app, real-time control. A headless **ezdxf** backend reads and writes DXF files anywhere — Linux, macOS, CI runners — with no AutoCAD installation required. Same tool surface, same response shapes, two execution modes.
2. **Built for agents.** Every tool has a typed schema, a deterministic error contract, and structured progress updates. No string-parsing tricks; no positional-argument guessing. Pydantic on the way in, dataclasses on the way out.
3. **Hardened by default.** Path traversal, command injection, AutoLISP escape, transaction-driven memory blowups, COM hangs — all addressed in the default configuration. You opt **in** to risk, never out.

---

## Highlights

- **Dual Engine Architecture**
  - **COM backend**: live AutoCAD control via the Win32 COM API, routed through a single-thread STA executor with a per-call timeout so an unresponsive AutoCAD never hangs the server.
  - **ezdxf backend**: headless DXF file operations powered by [ezdxf](https://github.com/mozman/ezdxf). Works on every platform, ideal for batch workloads and CI pipelines.
  - Automatic backend selection, with a clean override via `AUTOCAD_MCP_BACKEND`.

- **110 Tools, 12 Categories**
  - Drawing management — `drawing_new`, `drawing_open`, `drawing_save`, `drawing_save_as`, `drawing_export_dxf`, `drawing_export_pdf`, `drawing_purge`, `drawing_audit`, `drawing_undo`, `drawing_redo`, `drawing_close`
  - Entity creation — line, circle, arc, polyline, rectangle, text, mtext, hatch, spline, ellipse, point, block reference, batch create
  - Dimensions — linear, aligned, angular, radius, diameter
  - Entity modification — move, copy, rotate, scale, mirror, offset, delete, rectangular array, polar array, batch modify, set properties
  - Layer management — full lifecycle: create, delete, modify, freeze/thaw, lock/unlock, hide/show, isolate, set current
  - Block operations — list, insert, explode, attribute get/set, create-from-entities, find references
  - Analysis — entity stats, region select, distance/area measurement, bounding box, select by type/layer, layer statistics
  - View control — zoom extents, zoom window, screenshot, combined zoom-and-screenshot
  - Transactions — begin, commit, rollback, with disk-backed snapshots
  - System — status, get/set variables, run command, run AutoLISP, about
  - Templates and validation — apply standard layer templates, validate drawings against rule sets
  - Engineering / deterministic CAD — involute gear front view + section A-A, DIN 6885 keyed bore, ISO A3 title block, and the 8-step `drawing_finalize` gate
  - Premium drafting workflow — `drawing_plan`, deterministic OSNAP (`point_from_snap` / `point_intersection` / `point_tangent`), `drawing_apply_iso_layers`, `dimension_auto`, `entity_select_smart`, `drawing_critique`, `construction_*`

- **Production-Grade Plumbing**
  - FastMCP 3.0 lifespan-managed backend singleton
  - Middleware stack: error handling, audit logging, timing, request logging
  - Structured progress reports for long-running operations (`drawing_open`, exports, batch ops)
  - 318 tests across drawing, entity, dimension, layer, block, analysis, batch/template, engineering, premium, mocked-COM, and security suites
  - Ruff-clean codebase, GitHub Actions CI

- **Closed-Loop Quality Gate**
  - `drawing_finalize` runs **both** the 8-step structural validator **and** the premium ISO-128 critique (lineweights, layer colors, untrimmed corners, duplicate entities, leftover construction, dimension overlap) — leftover scaffolding or a non-standard lineweight blocks the gate instead of slipping through.
  - Deterministic geometry: `point_from_snap` / `point_intersection` / `point_tangent` give the agent exact OSNAP coordinates instead of guessed ones — the single biggest source of LLM drawing errors.
  - Cross-backend parity: the same `EntityInfo.properties` contract, save-format-from-extension, offset side selection, and ARC length on **both** engines.

- **Security-First Defaults**
  - Path traversal & system-directory protection
  - AutoLISP allowlist with regression tests for known bypass patterns (`vla-SendCommand`, `acet-*`, alias tricks, `funcall` indirection, `c:` custom commands, error-handler hijacks)
  - Command sanitization for `system_run_command`
  - HTTP transport refuses non-loopback bind without an explicit opt-in **and** a configured auth token
  - Configurable file size limit on DXF input (`MAX_DXF_BYTES`) blocks decompression-bomb-style attacks
  - Per-call COM timeout (`COM_CALL_TIMEOUT`) prevents a hung AutoCAD from freezing the server

---

## AI Client Compatibility

AutoCAD MCP Pro implements the Model Context Protocol. **If a host can speak MCP, it can drive this server.** No model-specific code, no per-vendor patches — the contract is the tool schema, and any MCP-aware client (and any LLM behind it) inherits the full 87-tool surface for free.

| Host                        | Status      | Notes                              |
|-----------------------------|-------------|------------------------------------|
| Claude Desktop              | Verified    | Native MCP support                 |
| Cursor                      | Verified    | Native MCP support                 |
| Cline / Roo Code (VS Code)  | Verified    | Native MCP support                 |
| Continue                    | Verified    | MCP via configuration              |
| Goose                       | Verified    | MCP via configuration              |
| Custom MCP clients          | Supported   | STDIO and HTTP transports          |

---

## Quickstart

```bash
git clone https://github.com/U-C4N/autocad-mcp.git
cd autocad-mcp
pip install -e ".[full]"
python server.py
```

That is it. The server starts in STDIO mode and your MCP client will discover all 110 tools.

For headless / CI workflows:

```bash
AUTOCAD_MCP_BACKEND=ezdxf python server.py
```

For HTTP transport (local-only by default — see [Security Model](#security-model)):

```bash
fastmcp run server.py:mcp --transport http --port 8000
```

---

## Installation

### Core (ezdxf backend only — works everywhere)

```bash
pip install -e .
```

### With live AutoCAD control (Windows + AutoCAD)

```bash
pip install -e ".[com]"
```

### With PDF export and matplotlib-based screenshots

```bash
pip install -e ".[pdf]"
```

### Everything

```bash
pip install -e ".[full]"
```

### Development

```bash
pip install -e ".[full]"
pip install pytest pytest-asyncio pytest-cov ruff
```

---

## MCP Client Configuration

### Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "autocad": {
      "command": "python",
      "args": ["C:/path/to/autocad-mcp/server.py"],
      "env": {
        "AUTOCAD_MCP_BACKEND": "auto",
        "ALLOWED_PATHS": "C:/Users/you/Documents/AutoCAD"
      }
    }
  }
}
```

### Cursor

`Settings → MCP → Add new server` and point at `python /path/to/server.py`.

### Cline / Continue / Goose

All standard MCP `command` + `args` configuration; same shape as Claude Desktop.

---

## Backend Selection

| `AUTOCAD_MCP_BACKEND` | Behavior                                                                              |
|-----------------------|---------------------------------------------------------------------------------------|
| `auto` *(default)*    | Try COM first on Windows, fall back to ezdxf if AutoCAD is not reachable.             |
| `com`                 | Force COM. Fails fast if AutoCAD is not running.                                      |
| `ezdxf`               | Force headless ezdxf. Works on Linux, macOS, Windows with no AutoCAD.                 |

---

## Configuration Reference

Copy `.env.example` to `.env` and edit:

| Variable                       | Default              | Purpose                                                               |
|--------------------------------|----------------------|-----------------------------------------------------------------------|
| `AUTOCAD_MCP_BACKEND`          | `auto`               | Backend selection (`auto`, `com`, `ezdxf`).                           |
| `LOG_LEVEL`                    | `INFO`               | Standard Python logging level.                                        |
| `ALLOWED_PATHS`                | *(empty)*            | Comma-separated absolute paths the server may read/write.             |
| `MAX_UNDO_STACK`               | `5`                  | Maximum number of in-flight transaction snapshots.                    |
| `MAX_DXF_BYTES`                | `52428800` (50 MB)   | Reject DXF files larger than this on `drawing_open`. `0` to disable.  |
| `MAX_LIST_LIMIT`               | `5000`               | Hard cap for `entity_list` and `analysis_select_*` responses.         |
| `COM_CALL_TIMEOUT`             | `60` (seconds)       | Per-call timeout for COM operations. Prevents a hung AutoCAD blocking the server. |
| `DANGEROUS_COMMANDS_ENABLED`   | `false`              | Disable command and AutoLISP sanitization. **Loud warning at startup.** |
| `ALLOW_REMOTE_HTTP`            | `false`              | Permit HTTP transport on non-loopback hosts.                          |
| `MCP_AUTH_TOKEN`               | *(empty)*            | Required when `ALLOW_REMOTE_HTTP=true`. Bearer token for HTTP clients. |

---

## Tool Catalog

| Group               | Count | Sample tools                                                              |
|---------------------|------:|---------------------------------------------------------------------------|
| Drawing             |    11 | `drawing_new`, `drawing_open`, `drawing_save`, `drawing_export_pdf`, `drawing_purge`, `drawing_close` |
| Entity Creation     |    13 | `entity_create_line`, `entity_create_polyline`, `entity_create_hatch`, `entity_create_block_ref`      |
| Dimensions          |     5 | `dimension_linear`, `dimension_aligned`, `dimension_angular`, `dimension_radius`, `dimension_diameter`|
| Entity Modification |    10 | `entity_move`, `entity_rotate`, `entity_array_polar`, `entity_offset`, `entity_array_rectangular`     |
| Entity Query        |     3 | `entity_get`, `entity_list`, `entity_delete_many`                                                     |
| Layer Management    |    12 | `layer_create`, `layer_freeze`, `layer_isolate`, `layer_modify`                                       |
| Block Operations    |     7 | `block_insert`, `block_explode`, `block_create_from_entities`, `block_find_references`                |
| Analysis            |     8 | `analysis_entity_stats`, `analysis_bounding_box`, `analysis_select_by_layer`, `analysis_layer_stats`  |
| View                |     4 | `view_zoom_extents`, `view_screenshot`, `view_zoom_and_screenshot`                                    |
| Transactions        |     3 | `transaction_begin`, `transaction_commit`, `transaction_rollback`                                     |
| System              |     6 | `system_status`, `system_run_command`, `system_run_lisp`, `system_about`                              |
| Batch / Templates   |     5 | `entity_batch_create`, `entity_batch_modify`, `template_apply_layers`, `validation_check`             |

`system_about` returns the live tool inventory — the count above is illustrative; the runtime number is authoritative.

---

## Architecture

```
server.py                  FastMCP 3.0 server (lifespan, middleware, tools)
│
├── config.py              Centralized environment-driven configuration
├── security.py            Path validation, command + LISP sanitization
│
└── backends/
    ├── base.py            AutoCADBackend ABC + shared dataclasses
    │                      (EntityInfo, LayerInfo, BlockInfo, DrawingInfo, CommandResult)
    ├── ezdxf_backend.py   Headless DXF/DWG via ezdxf
    │                      sync calls wrapped in asyncio.to_thread
    │                      transaction snapshots persisted to temp files
    └── com_backend.py     Live AutoCAD via pywin32
                           single-thread STA executor with per-call timeout
                           UUID-named selection sets, leak-free cleanup
```

The server boots once, the backend is initialized in the FastMCP lifespan, and every tool call is dispatched through the middleware stack:

`ErrorHandlingMiddleware → AuditMiddleware → TimingMiddleware → LoggingMiddleware → tool`

Tool returns are flowed back through `_dc()`, which converts dataclasses to JSON-serializable dicts and preserves the typed contract on the wire.

---

## Security Model

AutoCAD MCP Pro assumes a hostile-input threat model: the MCP client may be untrusted, the model behind it may be jailbroken, and the inputs to every tool may be adversarial.

- **AutoLISP allowlist** — `security.py` blocks the full ActiveX/COM family (`vla-*`, `vlax-*`, `vlr-*`), Express Tools (`acet-*`), command channels (`command`, `command-s`, `vl-cmdf`), file I/O, code loading, indirection vectors (`funcall`, `function`, `apply`, `eval`), custom command invocation (`c:*`), and error-handler hijacks. Every known bypass is covered by a regression test.
- **Command sanitization** — destructive AutoCAD verbs (`ERASE`, `PURGE`, `SHELL`, `SCRIPT`, `APPLOAD`, `NETLOAD`, `VBARUN`, …) are rejected unless `DANGEROUS_COMMANDS_ENABLED=true` is set, in which case the server logs a loud `WARNING` at startup and surfaces an `unsafe_mode` flag in `system_status` and `system_about`.
- **Path validation** — every tool that takes a path runs through `validate_path()`, which blocks traversal patterns, system directories, and constrains writes to a configurable `ALLOWED_PATHS` list.
- **HTTP transport guard** — by default the server refuses to bind anything other than `127.0.0.1`. To bind a non-loopback host you must opt in via `ALLOW_REMOTE_HTTP=true` **and** set `MCP_AUTH_TOKEN`.
- **Resource exhaustion guards** — `MAX_DXF_BYTES` rejects oversize input, `MAX_LIST_LIMIT` caps response sizes, transaction snapshots persist to disk so memory stays bounded, and `COM_CALL_TIMEOUT` keeps a hung AutoCAD from taking the server with it.

If you find a security issue, please open a private contact rather than a public issue.

---

## Benchmarks

A reproducible benchmark suite is in active development. Targets:

- **Drawing scale** — 1k / 10k / 100k entity workloads
- **Workload classes** — bulk creation, mass-modify, region select, render-and-screenshot, end-to-end design pass
- **Backend variants** — ezdxf on CPython 3.11/3.12, COM against AutoCAD 2024+
- **Model compatibility** — tool-call success rate and prompt token usage by model, on a fixed set of canonical CAD prompts

Numbers will land in [`benchmarks/`](benchmarks/) with a one-command runner and a hardware footprint. The table below will be updated as new models and AutoCAD versions release.

| Workload                       | ezdxf (3.12) | COM (AutoCAD 2025) | Notes |
|--------------------------------|--------------|--------------------|-------|
| Open + audit 10k-entity DXF    | *coming*     | *coming*           |       |
| Bulk-create 5k LINE entities   | *coming*     | *coming*           |       |
| Region select on 100k drawing  | *coming*     | *coming*           |       |
| End-to-end "design pass" prompt| *coming*     | *coming*           | by model family |

> Have a workload you want included? Open an issue.

---

## Development

### Run tests

```bash
pytest -v
```

### Coverage report

```bash
pytest --cov=. --cov-report=term-missing
```

### Lint and format

```bash
ruff check .
ruff format --check .
```

### Project structure

```
autocad-mcp/
├── server.py
├── config.py
├── security.py
├── backends/
│   ├── base.py
│   ├── ezdxf_backend.py
│   └── com_backend.py
├── tests/
│   ├── test_drawing.py
│   ├── test_entity_creation.py
│   ├── test_dimensions.py
│   ├── test_layer_block.py
│   ├── test_analysis.py
│   ├── test_batch_template.py
│   ├── test_ezdxf_backend.py
│   └── test_security.py
├── pyproject.toml
├── Dockerfile
└── .github/workflows/ci.yml
```

---

## Roadmap

- [x] **1.0** — Initial public release
- [x] **1.1** — Correctness, cross-backend parity & the enforced quality gate: `drawing_finalize` now runs the premium ISO-128 critique; deterministic geometry (`point_intersection`/`point_tangent`); dimension/save-format/polar-array/offset fixes across both engines; COM robustness (CoUninitialize, transaction & LISP guards); security hardening (HTTP bind guard on every launch path, LISP-allowlist bypass tests); mocked-COM test harness. **318 tests.**
- [ ] **1.2** — Closed-loop validation moat: scalar drawing-score, pre-plan clarification pass, and an iterative critique→repair→re-critique refiner (with dedicated transaction-stack isolation)
- [ ] **1.3** — ISO production: 2D GD&T (ISO 1101) feature-control frames, ISO 129 dimension tolerances/fits, ISO-25 dimension styles
- [ ] **1.4** — Public benchmark suite (`benchmarks/` with reproducible runner)
- [ ] **1.3** — Backend `capabilities()` map and per-tool enablement (e.g. hide `system_run_command` automatically on ezdxf)
- [ ] **1.4** — Module split for `server.py` and a clean `services/` orchestration layer
- [ ] **1.5** — Windows CI matrix with mocked COM backend; full coverage report
- [ ] **2.0** — Multi-document context, xref-aware tools, layout/paper-space first-class support
- [ ] **2.x** — Native Linux/macOS support for live control via [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) for round-trip DWG

---

## Requirements

- Python **3.11+**
- [FastMCP](https://github.com/jlowin/fastmcp) **3.0+**
- [ezdxf](https://github.com/mozman/ezdxf) **1.3+**
- *(optional, COM backend)* `pywin32` **306+**, AutoCAD 2020 or newer
- *(optional, screenshots)* `Pillow` **10.0+**
- *(optional, PDF export)* `matplotlib` **3.8+**

---

## Contributing

Pull requests are welcome — especially around the COM backend (mocking, additional Windows tooling), benchmark suite contributions, and additional layer / dimension standards.

For non-trivial changes, please open an issue first to align on direction.

---

## Author

**Umutcan Edizsalan**
Mechanical / engineering work at **Anka-Makine**.
Builds tools that survive contact with real production drawings.

GitHub: [@U-C4N](https://github.com/U-C4N)

This project exists because it earned its place in my own daily workflow. I will keep it maintained and aligned with my real usage.

---

## License

MIT — see [LICENSE](LICENSE).

Copyright © 2026 Umutcan Edizsalan.
