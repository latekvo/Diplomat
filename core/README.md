# `core/` — the shared, language-neutral source of truth

Everything in here is consumed **verbatim** by both front-ends:

- the macOS SwiftUI menu-bar app (`Sources/ArgentUtilsCore` loads it), and
- the Linux Qt6/PySide6 tray applet (`linux/argent_utils` loads it).

The two UIs differ only in *rendering*. All the triage logic — what to query,
how to filter, what the review prompt says — lives here once, so the apps can
never drift. Change a query or a threshold in one file and both platforms pick
it up.

| File | What it holds |
|------|---------------|
| `config.json` | repo coordinates (`owner` / `repo`) |
| `graphql/viewer.graphql` | `{ viewer { login } }` |
| `graphql/prs.graphql` | open-PR query (uses `$owner`/`$name` variables) |
| `graphql/issues.graphql` | open-issue query (uses `$owner`/`$name` variables) |
| `catalog.json` | the six tools: id, title, subtitle, icon (`sfSymbol` for macOS, `emoji` for Linux), colour (`color` name for macOS, `colorHex` for Linux), in display order |
| `filters.json` | filter constants: skill-file suffix, installer path prefixes, team/org associations, stale-ready day threshold, the `APPROVED` sentinel |
| `review.json` | the Review-PRs prompt model: depth levels + scope/action text blocks the wizard assembles |
| `conflicts.json` | the Resolve-conflicts prompt model: scope templates + the merge/resolve action blocks the wizard assembles |

## Contract notes

- **GraphQL variables, not interpolation.** The PR/issue queries declare
  `$owner`/`$name`; each front-end passes them via `gh ... -f owner=… -f name=…`
  so the query text stays repo-agnostic.
- **Icons/colours are intentionally dual.** `sfSymbol`+`color` are the macOS
  (SF Symbols + SwiftUI semantic colours) assets; `emoji`+`colorHex` are the
  Linux assets. These are rendering choices, not logic — both are kept here so
  the catalog stays a single list.
- **`_comment` keys** are documentation only; loaders ignore unknown keys.
- **Prompt assembly** (which blocks appear, in what order, under which toggles)
  is the only logic that lives as a thin ~20-line glue layer in each front-end
  (`ReviewConfig.buildPrompt` in Swift, `ReviewConfig.build_prompt` in Python) —
  identical by construction, and covered by tests on both sides.
