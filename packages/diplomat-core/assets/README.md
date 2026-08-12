# `assets/` — the shared, language-neutral source of truth

Everything in here is consumed **verbatim** by both front-ends:

- the macOS SwiftUI menu-bar app (`../Sources/DiplomatCore` loads it), and
- the Linux Qt6/PySide6 tray applet (`diplomat-platform/linux/diplomat_app` loads it).

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
| `models.json` | the exception lists behind `../Sources/DiplomatCore/AgentModel.swift`, which names the model a spawn will run on in the attribution tag every posted comment opens with (`[Diplomat, Opus 5]: …`): ids that name no single model, leading id segments that name a vendor rather than a model, and the whole segments that are initialisms. The general shape of an id is handled by rules in that file, so only what no rule can derive lives here |
| `audit-categories.json` | the **activity-feed** taxonomy (unrelated to `audit.json`): maps each raw audit action verb to one of the ten categories the panel's filter chips toggle, with per-platform icon + tint. Canonical mirror of `../Sources/DiplomatCore/AuditCategory.swift`, which stays the source of truth for the exhaustive Swift switch |
| `mesh.json` | Diplomat's half of the LAN P2P mesh model — the deployment overlay [SzpontNet](../../szpontnet-core/README.md) merges over its own `netmodel.json`: the duty catalog (which job classes the mesh routes, with per-duty platform spread — e.g. the audit's one-linux-plus-one-macos), the placement strategies (weakest-first / strongest-first / local-first / surplus-first), the platform / token / trust vocabularies and the tier labels. Everything in it is drawn on screen by both topology panels. The wire constants and the quota `accounts` model are the node's business and live in `szpontnet-core/szpontnet/netmodel.json` |
| `telemetry.json` | the Telemetry screen's model: where the ledger and the transcript-scan cursor live, how often a quota sample is taken and how much history is kept, the lookback ranges (7/14/30/60, default 14), the histogram/series resolution, the confidence level — and the ten metrics with their per-platform icon + tint. The arithmetic over the ledger is `../Sources/DiplomatCore/Telemetry.swift`, mirrored by `diplomat_runtime/telemetry.py` and diffed field-for-field by `diplomat-platform/linux/tests/test_telemetry_parity.py` |
| `golden-prompts/` | canonical prompt outputs, one `.txt` per mode; regenerate with `DIPLOMAT_GOLDEN_WRITE=1 swift run DiplomatCoreSmoke`, asserted byte-for-byte by the Swift smoke test AND `diplomat-platform/linux/tests/test_golden_prompts.py` |

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
- **The six tool lists are the one thing NOT single-sourced.** `ToolData.items`
  (`../Sources/DiplomatCore/ToolKind.swift`) and `Store.items_for`
  (`diplomat-platform/linux/diplomat_app/store.py`) each render the same six lists, down to the text
  of every row, because the lists are rebuilt on every render and neither side can
  afford a shell-out per render the way prompt assembly can. What stands in for
  single-sourcing is `diplomat-platform/linux/tests/test_tooldata_parity.py`: it drives both
  implementations over one fixture (via `diplomat-core tool-data`) and diffs the
  rows, so a format, filter, sort or pluralisation change on one platform fails CI
  until it is made on the other. **Change one, change both.**
- **Prompt assembly is single-sourced in Swift.** `buildPrompt` in `DiplomatCore`
  is the only implementation: the Linux applet does *not* re-implement it in
  Python, it shells out to the `diplomat-core` CLI
  (`diplomat-runtime/diplomat_runtime/promptcore.py` → `../Sources/DiplomatCoreCLI`), so the two
  front-ends are identical by construction rather than by convention. The
  `golden-prompts/` assertions on both sides remain as the regression net over
  that one builder (and over the Python→CLI bridge): a drift fails a CI job
  before it ships. Build the binary with `packages/diplomat-platform/linux/install/build-core.sh`.
- **The known-author single-PR tier** in `review.json`
  (`specific.mineOnly` / `specific.theirsOnly` / `specific.reviewerFindingsFirst`
  and `blocks.noVerdict`) exists because the monitors always know the PR's author,
  so they skip the author-poll CASE A/B prompt the wizards use. It has no golden
  files of its own.
