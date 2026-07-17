"""Full-E2E-test config + prompt builder.

The prompt text (scope + action blocks) all comes from the shared ``core/audit.json``;
only the *assembly* order/conditions live here as a thin glue layer, identical to
AuditConfig's ``buildPrompt`` in CoMaintainerCore. The terminal spawner is shared with
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
        # Single-sourced in Swift (CoMaintainerCore) via the co-maintainer-core CLI.
        from . import promptcore

        return promptcore.build_prompt({
            "kind": "audit",
            "fixIssues": self.fix_issues,
            "openPRs": self.open_prs,
        })
