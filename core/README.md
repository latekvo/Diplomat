# `core/` — the shared, language-neutral source of truth

Everything in here is consumed **verbatim** by both front-ends:

- the macOS SwiftUI menu-bar app (`Sources/DiplomatCore` loads it), and
- the Linux Qt6/PySide6 tray applet (`linux/diplomat_app` loads it).

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
| `audit.json` | the Full-E2E-test prompt model: scope + action blocks (find-only / fix open bug issues / open a PR per finding), plus the always-on HIGH/MEDIUM/LOW severity classification |
| `audit-categories.json` | the **activity-feed** taxonomy (unrelated to `audit.json`): maps each raw audit action verb to one of the ten categories the panel's filter chips toggle, with per-platform icon + tint. Canonical mirror of `Sources/DiplomatCore/AuditCategory.swift`, which stays the source of truth for the exhaustive Swift switch |
| `mesh.json` | the LAN P2P mesh model: protocol constants (discovery/heartbeat ports + timings, foreign accept/reminder deadlines), the duty catalog (which job classes the mesh routes, with per-duty platform spread — e.g. the audit's one-linux-plus-one-macos), the placement strategies (weakest-first / strongest-first / local-first / surplus-first), the three trust levels + the zero-trust `default`, and the quota `accounts` model. Consumed by the Python mesh node (`linux/diplomat_app/mesh`) and both topology panels; a future Swift node reads the same file |
| `golden-prompts/` | canonical prompt outputs, one `.txt` per mode; regenerate with `DIPLOMAT_GOLDEN_WRITE=1 swift run DiplomatCoreSmoke`, asserted byte-for-byte by the Swift smoke test AND `linux/tests/test_golden_prompts.py` |

## Contract notes

- **GraphQL variables, not interpolation.** The PR/issue queries declare
  `$owner`/`$name` and the monitor queries `$q` (+`$withFiles`); each front-end
  passes them via `gh api graphql -f …` so the query text stays repo-agnostic.
  Both applets now run the monitors, so both execute all five.
- **Icons/colours are intentionally dual.** `sfSymbol`+`color` are the macOS
  (SF Symbols + SwiftUI semantic colours) assets; `emoji`+`colorHex` are the
  Linux assets. These are rendering choices, not logic — both are kept here so
  the catalog stays a single list.
- **`_comment` keys** are documentation only; loaders ignore unknown keys.
- **Prompt assembly is single-sourced in Swift.** `buildPrompt` in `DiplomatCore`
  is the only implementation: the Linux applet does *not* re-implement it in
  Python, it shells out to the `diplomat-core` CLI
  (`linux/diplomat_app/promptcore.py` → `Sources/diplomat-core`), so the two
  front-ends are identical by construction rather than by convention. The
  `golden-prompts/` assertions on both sides remain as the regression net over
  that one builder (and over the Python→CLI bridge): a drift fails a CI job
  before it ships. Build the binary with `linux/scripts/build-core.sh`.
- **The known-author single-PR tier** in `review.json`
  (`specific.mineOnly` / `specific.theirsOnly` / `specific.reviewerFindingsFirst`
  and `blocks.noVerdict`) exists because the monitors always know the PR's author,
  so they skip the author-poll CASE A/B prompt the wizards use. It has no golden
  files of its own.
