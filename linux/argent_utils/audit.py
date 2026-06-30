"""Full-E2E-test config + prompt builder.

The prompt text (scope + action blocks) all comes from the shared ``core/audit.json``;
only the *assembly* order/conditions live here as a thin glue layer, identical to
AuditConfig's ``buildPrompt`` in ArgentUtilsCore. The terminal spawner is shared with
:mod:`review` (``review.spawn`` / ``review.resolved``).

Unlike Review / Resolve-conflicts there is no whose-PRs axis: the audit always
targets the whole repository. Two independent toggles gate the optional scope:

* ``fix_issues`` — also reproduce + fix the repo's OPEN BUG issues (never feature
  requests), in addition to auditing the existing code.
* ``open_prs`` — open a focused PR for every confirmed finding / fix. When off the
  run is a read-only audit that only reports its hard-reproduced findings.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import core


@dataclass
class AuditConfig:
    me: str = ""  # authenticated viewer login, used as the @handle for authoring
    fix_issues: bool = False
    open_prs: bool = False

    @property
    def target_repo(self) -> tuple[str, str]:
        """The configured target repo (owner, repo), from the shared core config."""
        cfg = core.config()
        return cfg["owner"], cfg["repo"]

    @property
    def is_valid(self) -> bool:
        """A whole-repo audit needs no user input, so it is always spawnable."""
        return True

    def build_prompt(self) -> str:
        owner, repo = self.target_repo
        blocks_src = core.audit()["blocks"]

        def fill(s: str) -> str:
            return s.format(owner=owner, repo=repo)

        blocks: list[str] = [fill(blocks_src["intro"]), fill(blocks_src["bar"])]
        # Always: classify every finding H/M/L (drives the report + the Low<20-LOC PR gate).
        blocks.append(fill(blocks_src["classify"]))
        # Optional: also reproduce + fix the repo's open BUG issues.
        if self.fix_issues:
            blocks.append(fill(blocks_src["issues"]))
        # Delivery: open a PR per fix, or stay read-only and just report.
        if self.open_prs:
            blocks.append(fill(blocks_src["openPRs"]))
            blocks.append(fill(blocks_src["noAttribution"]))
        else:
            blocks.append(fill(blocks_src["readOnly"]))
        blocks.append(fill(blocks_src["summary"]))
        return "\n\n".join(blocks)
