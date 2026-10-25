# autocad-mcp-pro — Claude Code plugin

The AutoCAD MCP Pro server and its premium drafting skill, installed together.

```text
/plugin install autocad-mcp-pro --marketplace U-C4N/Autocad-MCP
```

(Claude Code 2.1.275 or later; on an older one, `/plugin marketplace add U-C4N/Autocad-MCP`
then `/plugin install autocad-mcp-pro@autocad-mcp`.) From a shell the same commands
are `claude plugin marketplace add U-C4N/Autocad-MCP` and
`claude plugin install autocad-mcp-pro@autocad-mcp`.

## What it ships

| Part | Where | What it does |
|---|---|---|
| MCP server `autocad` | `.mcp.json` | Runs `uvx --from "autocad-mcp-pro[com,pdf,office]==<version>" autocad-mcp` — the PyPI package at exactly this plugin's version, with the live-AutoCAD (`com`, Windows only), PDF/screenshot (`pdf`) and workbook (`office`) extras. Needs [uv](https://docs.astral.sh/uv/). |
| Skill `premium` | `skills/premium/` | The measured workflow: requirements → geometry → verified on the CAD → saved → reopened → delivered, a decision table by source type, and the shortcuts that produce confident wrong answers. Claude loads it on its own for drawing work; invoke it directly as `/autocad-mcp-pro:premium`. |
| Eval cases | `evals/` | Twenty negative scenarios for `claude plugin eval` (wrong label, two equal holes, schematic metres, prompt injection in drawing text, a saved file confirmed from memory, ...); see `evals/README.md` and, for running them, `benchmarks/skill_eval/README.md` in the repository. Not loaded at run time. |

The backend is chosen at start-up as in the main README: live AutoCAD on Windows
when it is running, headless `ezdxf` otherwise. Set `AUTOCAD_MCP_BACKEND`,
`ALLOWED_PATHS` and the other server variables in the environment Claude Code
starts from.

## Running the server from a checkout (local-source override)

The pinned version is what a user gets, but it reaches PyPI only at release, and
development and evaluation need the code in front of them. The server's source is
one variable:

```text
AUTOCAD_MCP_SOURCE=C:/path/to/Autocad-MCP[com,pdf,office]
```

`.mcp.json` passes `${AUTOCAD_MCP_SOURCE:-autocad-mcp-pro[com,pdf,office]==<version>}`
to `uvx --from`, so with the variable set the plugin runs the checkout (any
`uvx --from` source works: a folder, a wheel, a `git+https://` URL); unset, it
runs the pinned release. Set it in the shell that starts Claude Code, or in the
`env` block of your Claude Code settings.

## Already running the server yourself?

Keep one server. Either turn off this plugin's server in `/mcp` (it is listed as
`plugin:autocad-mcp-pro:autocad`) and keep only its skill, or skip the plugin
and copy the skill folder — `.claude/skills/autocad-mcp-premium/` from the
repository — into your project's or your user's `.claude/skills/`.

## Generated copy

`skills/premium/` is generated from the canonical skill,
`.claude/skills/autocad-mcp-premium/`, by `scripts/build_plugin.py`; edit the
canonical folder and rerun the script. `python scripts/build_plugin.py --check`
fails on any difference, and `tests/test_plugin_package.py` holds the plugin's
version, the `==<version>` pin, the skill's links and every tool name it uses
against the package.
