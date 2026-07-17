# `core/` — the shared, language-neutral source of truth

Everything in here is consumed **verbatim** by both front-ends:

- the macOS SwiftUI menu-bar app (`Sources/CoMaintainerCore` loads it), and
- the Linux Qt6/PySide6 tray applet (`linux/co_maintainer` loads it).

The two UIs differ only in *rendering*. All the triage logic — what to query,
how to filter, what the prompts say — lives here once. Change a query or a
threshold in one file and both platforms pick it up; the golden-prompt tests
(below) fail CI if the two prompt builders ever produce different bytes.

| File | What it holds |
|------|---------------|
| `config.json` | repo coordinates (`owner` / `repo`) |
| `graphql/viewer.graphql` | `{ viewer { login } }` |
| `graphql/prs.graphql` | open-PR query (uses `$owner`/`$name` variables) |
| `graphql/issues.graphql` | open-issue query (uses `$owner`/`$name` variables) |
| `graphql/monitor-prs.graphql` | the PR auto-fix monitor's snapshot of my open PRs (search query in `$q`): mergeability, review verdict, per-thread resolution |
| `graphql/review-requests.graphql` | PRs requesting my review (`$q`), with the request/last-review timestamps; `$withFiles` optionally pulls changed paths for the verdict gate |
| `catalog.json` | the six tools: id, title, subtitle, icon (`sfSymbol` for macOS, `emoji` for Linux), colour (`color` name for macOS, `colorHex` for Linux), in display order |
| `filters.json` | filter constants: skill-file suffix, installer path prefixes, team/org/trusted associations, stale-ready day threshold, the `APPROVED` sentinel |
| `review.json` | the Review-PRs prompt model: depth levels + scope/action text blocks the wizard assembles |
| `conflicts.json` | the Resolve-conflicts prompt model: scope templates + the merge/resolve action blocks the wizard assembles |
| `audit.json` | the Full-E2E-test prompt model: scope + action blocks (find-only / fix open bug issues / open a PR per finding) |
| `mesh.json` | the LAN P2P mesh model: protocol constants (discovery/heartbeat ports + timings), the duty catalog (which job classes the mesh routes, with per-duty platform spread — e.g. the audit's one-linux-plus-one-macos), and the placement strategies (weakest-first / strongest-first / local-first). Consumed by the Python mesh node (`linux/co_maintainer/mesh`) and the topology panel; a future Swift node reads the same file |
| `golden-prompts/` | canonical prompt outputs, one `.txt` per mode; regenerate with `CO_MAINTAINER_GOLDEN_WRITE=1 swift run CoMaintainerCoreSmoke`, asserted byte-for-byte by the Swift smoke test AND `linux/tests/test_golden_prompts.py` |

## Contract notes

- **GraphQL variables, not interpolation.** The PR/issue queries declare
  `$owner`/`$name` and the monitor queries `$q` (+`$withFiles`); each front-end
  passes them via `gh api graphql -f …` so the query text stays repo-agnostic.
  The two monitor queries are currently only *executed* by the macOS applet
  (the monitors are macOS-only), but they live here with the rest so a future
  Linux monitor reuses them as-is.
- **Icons/colours are intentionally dual.** `sfSymbol`+`color` are the macOS
  (SF Symbols + SwiftUI semantic colours) assets; `emoji`+`colorHex` are the
  Linux assets. These are rendering choices, not logic — both are kept here so
  the catalog stays a single list.
- **`_comment` keys** are documentation only; loaders ignore unknown keys.
- **Prompt assembly** (which blocks appear, in what order, under which toggles)
  is the only logic that lives as a glue layer in each front-end
  (`buildPrompt` in Swift, `build_prompt` in Python). The parity guarantee is
  not "by construction" but enforced: every mode both sides can assemble is
  compared byte-for-byte against `golden-prompts/` in both test suites, so a
  drift fails one CI job before it ships.
- **The known-author single-PR tier** in `review.json`
  (`specific.mineOnly` / `specific.theirsOnly` / `specific.reviewerFindingsFirst`
  and `blocks.noVerdict`) is currently consumed by the macOS side only - the
  monitors always know the PR's author, so they skip the author-poll CASE A/B
  prompt the wizards use. The Python builder doesn't assemble these modes (yet),
  which is also why they have no golden files.
