# Skill eval cases

Cases for `claude plugin eval` (Claude Code CLI 2.1.310), one folder each:
`prompt.md` (front matter: `description`, `tags`, `expected_outcome`,
`max_turns`, `timeout_seconds`, `allowed_tools`; the body is the prompt) and
`graders/*.md` (front matter `type:` `tool_used`, `regex` or `llm`; an `llm`
grader's body is its criteria). Every case is a negative scenario: a request
whose easy answer is confidently wrong. `graders/skill-fired.md`
(`tool_used: Skill`, `arm: with-only`) is the plugin-fired indicator, not part of
the score.

The plugin's tools are named `mcp__plugin_autocad-mcp-pro_autocad__<tool>`;
`allowed_tools` grants the whole server (`mcp__plugin_autocad-mcp-pro_autocad`),
which a run must also grant with `--allow-tools`. No case grants a shell: on a
Windows seat without the sandbox backend the CLI refuses any run that does, so
the cases reach files through the server's own tools.

Every run is paid and the CLI publishes its HTML report to claude.ai unless
told not to, so always pass `--no-publish`, a `--max-cost-usd` ceiling and, for
a check, one case (`--case <name> --runs 1`). Validating that the cases load
costs nothing: an unknown `--model` makes every run end on an API error after
the cases were parsed, and a case that does not load is reported as
"failed to load".

`tests/test_plugin_package.py` checks every case against the CLI's case schema,
that each MCP tool a grader names exists, and that the named scenarios of the
1.7.0 spec are covered.
