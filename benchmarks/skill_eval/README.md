# Skill A/B evaluation

Does the premium skill change what an agent does with the AutoCAD MCP tools?
The suite under [`plugin/evals/`](../../plugin/evals/README.md) answers with 20
negative scenarios — requests whose easy answer is confidently wrong (a label over
geometry that measures something else, two equal holes, a schematic read as
metres, a stale preview, a saved file confirmed from memory, ...) — run by Claude
Code's own `claude plugin eval` (CLI 2.1.310). Spec §6 Task 22 (B55).

Every run is paid. Nothing here runs in CI; the full budgeted run is the
maintainer's, locally, at release.

## The arms

| Arm | What the agent has | Built by |
|---|---|---|
| no plugin | neither the server nor the skill (the CLI's own baseline, `--ablation with-without`) | the CLI |
| server only | the AutoCAD server, no skill | `build_plugin.py --variant server-only` |
| `SKILL.md` only | the server and `SKILL.md`, without its references and examples | `build_plugin.py --variant skill-only` |
| full plugin | the server and the whole skill | `build_plugin.py --variant full` |

The CLI's no-plugin arm has no AutoCAD tools at all, so it measures the server as
much as the skill; the skill's effect is the difference between **server only**
and the two skill arms. Each `claude plugin eval` below also runs the no-plugin
baseline (`--ablation with-without`, the default) and reports the delta;
`--ablation none` skips it on the second and third runs to save money.

### Why the arms are generated, not `plugin/` itself

`claude plugin eval` hands its runs only the operator's `EVAL_*` variables (read
off the CLI 2.1.310 bundle). `AUTOCAD_MCP_SOURCE` and `AUTOCAD_MCP_BACKEND` never
reach the shipped `plugin/.mcp.json`, so its server would be the PyPI release on
the `auto` engine — the live AutoCAD on a seat that runs one, driving whatever
document is active. Each generated arm's `.mcp.json` runs **this checkout's**
`server.py` with this checkout's interpreter and `AUTOCAD_MCP_BACKEND=ezdxf`,
under the shipped plugin and server names (the graders' tool names depend on
both). `tests/test_skill_eval.py` starts that server and asks it which engine it
runs. The arms are written under the git-ignored `out/` (the script refuses a
folder git would track).

## Commands

Once per checkout, the interpreter the arms will run needs every extra:

```bash
uv run --frozen --all-extras python -c "import ezdxf, matplotlib, openpyxl"
```

Build the three arms (rebuild after any change to the skill or the cases):

```bash
uv run --frozen --all-extras python scripts/build_plugin.py --variant full --out out/plugin-full
uv run --frozen --all-extras python scripts/build_plugin.py --variant skill-only --out out/plugin-skill-only
uv run --frozen --all-extras python scripts/build_plugin.py --variant server-only --out out/plugin-server-only
```

Pin the model (a full model id, the same for every arm) and a cost ceiling per
arm, then run the arms. `--runs 3` is the default; the roadmap's 10 or more per
case is a budget decision made at run time.

```bash
export EVAL_MODEL=<full model id>
claude plugin eval out/plugin-full --model "$EVAL_MODEL" --runs 3 --allow-real-servers --mocks record --allow-tools mcp__plugin_autocad-mcp-pro_autocad --max-cost-usd 60 --json out/skill-eval-full.json --output-dir out/skill-eval-full --keep-temp --trust-plugin --no-publish
claude plugin eval out/plugin-skill-only --model "$EVAL_MODEL" --runs 3 --ablation none --allow-real-servers --mocks record --allow-tools mcp__plugin_autocad-mcp-pro_autocad --max-cost-usd 30 --json out/skill-eval-skill-only.json --output-dir out/skill-eval-skill-only --keep-temp --trust-plugin --no-publish
claude plugin eval out/plugin-server-only --model "$EVAL_MODEL" --runs 3 --ablation none --allow-real-servers --mocks record --allow-tools mcp__plugin_autocad-mcp-pro_autocad --max-cost-usd 30 --json out/skill-eval-server-only.json --output-dir out/skill-eval-server-only --keep-temp --trust-plugin --no-publish
```

One case, once — the check that the kit works end to end (this is what Task 22
ran; its result is below):

```bash
claude plugin eval out/plugin-full --case saved-not-in-memory --runs 1 --model sonnet --allow-real-servers --mocks record --allow-tools mcp__plugin_autocad-mcp-pro_autocad --max-cost-usd 2 --json out/skill-eval-smoke.json --trust-plugin --no-publish
```

The flags, and why each is there:

- `--no-publish` — always. The CLI publishes its HTML report to claude.ai unless
  told not to.
- `--max-cost-usd` — a hard ceiling; the CLI stops launching runs at it (exit 2)
  and reports the partial result.
- `--model` — pinned, so the arms compare the skill and not two models.
- `--allow-real-servers --mocks record` — the cases need the real server (there
  are no mocks); `record` would otherwise not start it.
- `--allow-tools mcp__plugin_autocad-mcp-pro_autocad` — the operator's grant for
  the server's tools; each case's `allowed_tools` names it too. No case grants a
  shell (refused on a Windows seat without the sandbox backend).
- `--trust-plugin` — answers the first-run trust prompt for a plugin built here.
- `--keep-temp` — keeps each run's folder, where `check_artifacts.py` finds the
  drawings it saved.

## Re-measuring the saved drawings

A grader sees the transcript and the files a run left — `file_exists` says
`bracket.dxf` was written — never what is inside a drawing. For the cases that
save one, `check_artifacts.py` opens the file fresh with `verify_artifact` (the
reader behind `drawing_verify_requirements(artifact_path=...)`) and measures it
against [`artifact_checks.json`](artifact_checks.json): what a correct run's file
holds, in the requirement's unit through the file's declared unit (a drawing
left in `drawing_new`'s default metres fails a millimetre check, as the prompt
asked for millimetres).

```bash
uv run --frozen --all-extras python benchmarks/skill_eval/check_artifacts.py --root <a kept run folder> --json out/skill-eval-artifacts.json
uv run --frozen --all-extras python benchmarks/skill_eval/check_artifacts.py --case saved-not-in-memory --file <path/to/bracket.dxf>
```

Each case is `as_expected`, `not_as_expected` (the rows say what was measured),
`missing` (the run saved nothing there — a failed case, never a skip) or
`unreadable`. Exit 0 when every case is `as_expected`, 1 otherwise, 2 on a usage
error.

## Paths inside a run

Every path a case names is relative to the run's folder and sits directly in it
(`./bracket.dxf`, `./delivery`): the server's save tools refuse a missing parent
folder, and a case grants no tool that creates one.

## Recorded runs

| Date | Command | Arm | Score | Turns | Cost (USD) | Graders |
|---|---|---|---|---|---|---|
| 2026-10-25 | the one-case run above (CLI 2.1.310, `--model sonnet`) | full plugin | 1.0 | 20 | 0.32 | `saved` pass (`bracket.dxf` in the run folder), `file-verified` pass (`drawing_verify_requirements` with `artifact_path`, 2 calls), `measured-on-file` pass (judge 3/3), `skill-fired` yes |
| 2026-10-25 | same | no plugin | 0.0 | 4 | 0.03 | every grader fails: no AutoCAD tools in this arm |

The run cost 0.36 USD in all (judge included), delta +1.0. `check_artifacts.py`
on the full arm's kept folder: `saved-not-in-memory: as_expected` - S1 40.0,
D1 10.0, D2 10.0, N1 2. One case, once, on an aliased model: evidence that the
kit runs end to end (the server started from the checkout, headless; the saved
file landed in the run folder where `file_exists` and `check_artifacts.py`
found it), not a measurement of the skill.

The same day all 20 cases were loaded with an unknown `--model`
(`--ablation none --runs 1`, the server-only arm): 20 cases parsed, every run
ended on the model error before any turn, cost 0.
