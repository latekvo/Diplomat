"""Fix-issues wizard — scope, depth, filters and action toggles, then SPAWN.

The Linux analogue of IssueWizard.swift. Collects the same choices — which of the
repo's open issues to work, how hard to prove each one, and what the run may do
about it — and builds the prompt from the shared assets/issues.json. Dispatching it
is :class:`~diplomat_app.wizardbase.SpawnWizard`'s job. Persistent widget (state
survives data refreshes).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QVBoxLayout,
)

from . import glyphs, issues, widgets
from .issues import Target
from .store import Store
from .wizardbase import SpawnWizard

_TINT = "#00C7BE"  # mint, matching the macOS Fix-issues card


class IssueWizardView(SpawnWizard):
    def __init__(self, store: Store) -> None:
        super().__init__(store, kind="issues", tint=_TINT)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        root.addWidget(widgets.wizard_title(glyphs.G_ISSUES, "Fix issues"))

        # Which issues: all / mine / one user's / the community's / the org's / one.
        self.target = QComboBox()
        for t in Target:
            self.target.addItem(t.title, t)
        self.target.currentIndexChanged.connect(self._sync)
        root.addWidget(self.target)

        self.mine_caption = QLabel("")
        self.mine_caption.setStyleSheet(widgets.muted(10))
        root.addWidget(self.mine_caption)

        # The username field (someone else's) and the single-issue field share this
        # slot; only the one matching the current scope shows (see _sync).
        self.username = QLineEdit()
        self.username.setPlaceholderText("github username")
        self.username.textChanged.connect(self._sync)
        root.addWidget(self.username)

        self.specific_issue = QLineEdit()
        self.specific_issue.setPlaceholderText("issue # or URL")
        self.specific_issue.textChanged.connect(self._sync)
        root.addWidget(self.specific_issue)

        self.issue_warning = widgets.wizard_warning()
        root.addWidget(self.issue_warning)

        root.addWidget(widgets.wizard_blurb(
            "Reproduces each issue, fixes it, and re-runs the same reproduction to "
            "prove the fix lands. Anything it can't reproduce is reported, never "
            "guessed at."
        ))

        # Depth
        depth_header = QHBoxLayout()
        dl = QLabel("Fix depth")
        dl.setStyleSheet(widgets.muted(10, bold=True))
        self.depth_title = QLabel()
        self.depth_title.setStyleSheet("font-weight: 700; font-size: 10px;")
        self.depth_title.setAlignment(Qt.AlignmentFlag.AlignRight)
        depth_header.addWidget(dl)
        depth_header.addWidget(self.depth_title, 1)
        root.addLayout(depth_header)

        self._depth_ids = issues.depth_ids()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(len(self._depth_ids) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.setValue(self._default_depth_index())
        self.slider.valueChanged.connect(self._sync)
        root.addWidget(self.slider)

        self.depth_blurb = widgets.wizard_blurb("")
        root.addWidget(self.depth_blurb)

        # Filters + action toggles
        self.unassigned_only = QCheckBox("Only unassigned issues")
        self.unassigned_only.setChecked(True)
        self.unassigned_only.setToolTip(
            "Skip every issue that already has an assignee — somebody is on it already."
        )
        self.assign_to_me = QCheckBox("Assign each issue to me while working it")
        self.assign_to_me.setChecked(True)
        self.assign_to_me.setToolTip(
            "Claim the issue on GitHub before starting, and hand it back if the run "
            "abandons it — what stops a second agent taking the same one."
        )
        self.open_prs = QCheckBox("Open a draft PR per fix")
        self.open_prs.setChecked(True)
        self.open_prs.setToolTip(
            "Off: nothing reaches the remote — each fix is left in the working tree "
            "and reported."
        )
        self.comment_on_issue = QCheckBox("Comment the outcome on the issue")
        self.comment_on_issue.setChecked(True)
        self.comment_on_issue.setToolTip(
            "One comment per issue actually worked: what was reproduced, the cause, "
            "and where the fix is."
        )
        for cb in (self.unassigned_only, self.assign_to_me, self.open_prs,
                   self.comment_on_issue):
            cb.toggled.connect(self._sync)
            root.addWidget(cb)

        self.include_features = widgets.wizard_escalation(
            glyphs.G_FINAL, "Also take on feature requests",
            "Off: only real bug reports are worked — every feature request, question "
            "and wishlist item is skipped.",
        )
        root.addWidget(self.include_features)

        # Mesh routing (visible only while the LAN mesh is enabled + running),
        # then SPAWN + its status line.
        self._add_dispatch_controls(root)

        root.addStretch(1)
        self._sync()

    # MARK: config from widgets

    def _default_depth_index(self) -> int:
        try:
            return self._depth_ids.index(issues.default_depth_id())
        except ValueError:
            return 0

    def _config(self) -> issues.IssueConfig:
        return issues.IssueConfig(
            depth=self._depth_ids[self.slider.value()],
            target=self.target.currentData(),
            username=self.username.text(),
            me=self.store.effective_me,
            specific_issue=self.specific_issue.text(),
            unassigned_only=self.unassigned_only.isChecked(),
            assign_to_me=self.assign_to_me.isChecked(),
            open_prs=self.open_prs.isChecked(),
            comment_on_issue=self.comment_on_issue.isChecked(),
            include_features=self.include_features.isChecked(),
        )

    def _sync(self) -> None:
        cfg = self._config()
        depth = issues.depth_by_id(cfg.depth)
        self.depth_title.setText(depth["title"])
        self.depth_blurb.setText(depth["blurb"])

        is_mine = cfg.target == Target.MINE
        is_specific = cfg.is_single_issue
        self.username.setVisible(cfg.target == Target.SOMEONE)
        self.specific_issue.setVisible(is_specific)

        me = self.store.effective_me
        self.mine_caption.setText(f"issues opened by @{me}" if (is_mine and me) else "")
        self.mine_caption.setVisible(bool(is_mine and me))

        ref = cfg.issue_ref
        if is_specific and ref.repo_mismatch:
            owner, repo = cfg.target_repo
            self.issue_warning.setText(f"That issue isn't in {owner}/{repo}.")
            self.issue_warning.setVisible(True)
        else:
            self.issue_warning.setVisible(False)

        self.unassigned_only.setVisible(cfg.can_filter_unassigned)

        self._restyle_spawn()

    def _label(self) -> str:
        """Names the scope and the depth, so the sessions list says which issues this
        run is working and how hard it is proving them."""
        cfg = self._config()
        if cfg.target == Target.SPECIFIC:
            n = cfg.issue_ref.number
            scope = f"#{n}" if n is not None else "issue"
        elif cfg.target == Target.SOMEONE:
            scope = f"@{cfg.username.strip() or 'user'}"
        elif cfg.target == Target.MINE:
            scope = "mine"
        elif cfg.target == Target.CONTRIBUTORS:
            scope = "contributors"
        elif cfg.target == Target.MEMBERS:
            scope = "members"
        else:
            scope = "all open"
        return f"Issues · {scope} · {issues.depth_by_id(cfg.depth)['title']}"

    def _author_login(self) -> str | None:
        """Whose issues this run touches, when it names one person — the pipeline's ban
        dimension. Nobody in particular for every other scope."""
        if self._config().target != Target.SOMEONE:
            return None
        return self.username.text().strip() or None
