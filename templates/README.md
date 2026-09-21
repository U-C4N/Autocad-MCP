# Bundled drawing templates

Five templates, built by the server's own tools — never hand-edited — and
listed by `drawing_template_list`. Start from one with
`drawing_new(template="iso_a3_mech")`.

| name | standard | sheet | layer set | styles | page setup |
|---|---|---|---|---|---|
| `iso_a3_mech` | ISO | A3 landscape, ISO 5457 frame + ISO 7200 title block | mech | ISO-25 / ISOCP | ISO_A3, monochrome.ctb, 1:1 |
| `iso_a1_arch` | ISO | A1 landscape | iso13567 | ISO-25 / ISOCP, annotation scale 1:50 | ISO_A1, monochrome.ctb, fit |
| `iso_a3_pid` | ISO | A3 landscape, ISO 5457 frame + title block | pid | ISO-25 / ISOCP | ISO_A3, monochrome.ctb, 1:1 |
| `ansi_b_mech` | ANSI (metric) | ANSI B landscape | mech | ANSI / ROMANS | ANSI_B, monochrome.ctb, 1:1 |
| `ansi_d_arch` | ANSI (metric) | ANSI D landscape | iso13567 | ANSI / ROMANS | ANSI_D, monochrome.ctb, fit |

## The two files per template

- `<name>.dxf` — the headless twin. Produced by
  `uv run --frozen python scripts/build_templates.py`, which runs
  `drawing_new → apply_layer_set → styles → drawing_settings → layout rename →
  titleblock_apply_iso_a3 (A3 sheets) → page_setup_apply → drawing_save_as`.
  `scripts/build_templates.py --check` rebuilds into a temporary folder and
  fails if any committed DXF differs (the save-time stamps `$TDCREATE`,
  `$TDUPDATE`, `$FINGERPRINTGUID`, `$VERSIONGUID` and the two `EZDXF_META`
  entries are masked; everything else must be byte-identical).
  `tests/test_templates.py` runs the same check.
- `<name>.dwt` — the live twin, used by `drawing_new(template=…)` on the COM
  backend. AutoCAD's DWT is a DWG container, which no headless library
  writes, so these are produced **on the live machine** by
  `uv run --frozen python scripts/smoke_settings_com.py --build-dwt` (Task 24's
  smoke): it opens each DXF over COM and calls `drawing_template_save(…dwt)`
  (`SaveAs(path, ac2018_Template)`), then the files are committed once. While
  a `.dwt` is missing, `resolve_template` falls back to the DXF and reports
  `source: "bundled_dxf"`; `drawing_template_list` shows `files.dwt.present`.
  The live engine does not open that DXF blank: `Documents.Add` accepts only
  a genuine DWT (measured on AutoCAD 2026), so the COM `drawing_new` converts
  the DXF through `SaveAs(…, ac2018_Template)` once, caches the result under
  the temp folder keyed on path, size and mtime, and reports
  `template_dwt: {path, cached}`.

Line endings: ezdxf writes CRLF and `.gitattributes` pins `templates/*.dxf`
to CRLF so a checkout on any platform equals a fresh build.

Rebuild after changing anything the build depends on (layer sets, the title
block, the dimension presets, the settings facade) and commit the result
together with the change — the reproducibility test is the gate.
