# Skill eval cases

Twenty cases for `claude plugin eval` (Claude Code CLI 2.1.310), one folder each:
`prompt.md` (front matter: `description`, `tags`, `expected_outcome`,
`max_turns`, `timeout_seconds`, `allowed_tools`; the body is the prompt) and
`graders/*.md` (front matter `type:` `tool_used`, `tool_order`, `regex`,
`file_exists` or `llm`; an `llm` grader's body is its criteria). Every case is a
negative scenario: a request whose easy answer is confidently wrong.
`graders/skill-fired.md` (`tool_used: Skill`, `arm: with-only`) is the
plugin-fired indicator, not part of the score.

A path a case names (`./bracket.dxf`, `./delivery`) is relative to the run's
folder and sits directly in it: the server's save tools refuse a missing parent
folder and no case grants a tool that creates one. `file_exists` graders check
the same relative path.

The plugin's tools are named `mcp__plugin_autocad-mcp-pro_autocad__<tool>`;
`allowed_tools` grants the whole server (`mcp__plugin_autocad-mcp-pro_autocad`),
which a run must also grant with `--allow-tools`. No case grants a shell: on a
Windows seat without the sandbox backend the CLI refuses any run that does, so
the cases reach files through the server's own tools.

Run the suite against the generated arms, never against this folder's plugin:
`claude plugin eval` passes only `EVAL_*` variables to its runs, so the shipped
`.mcp.json` would start the PyPI release on the `auto` engine - a live AutoCAD
where one runs. `scripts/build_plugin.py --variant full|skill-only|server-only
--out out/<arm>` writes an arm whose server is the checkout, headless;
[`benchmarks/skill_eval/README.md`](../../benchmarks/skill_eval/README.md) has the
commands, the flags and the recorded runs, and `check_artifacts.py` there
re-measures the drawings a run saved.

Every run is paid and the CLI publishes its HTML report to claude.ai unless
told not to, so always pass `--no-publish`, a `--max-cost-usd` ceiling and, for
a check, one case (`--case <name> --runs 1`). Validating that the cases load
costs nothing: an unknown `--model` makes every run end on an API error after
the cases were parsed, and a case that does not load is reported as
"failed to load".

`tests/test_plugin_package.py` checks every case against the CLI's case schema,
that each MCP tool a grader names exists (`tool_used` and both sides of
`tool_order`), that a `file_exists` path is one the prompt names, that the named
scenarios of the 1.7.0 spec are covered, that there are 20 cases and that every
grader type the spec names is used.
