"""The Argent Mesh topology column — read the LAN, configure the whole mesh.

The Linux face of Argent Mesh (see ``core/mesh.json`` for the model and
``argent_utils.mesh`` for the node). It renders the local node's public topology
snapshot (``~/.argent/mesh/state.json``): a compact wire graph of self + peers,
one editable card per node (tier / token state — edits apply to *any* node,
self or peer, forwarded over the mesh so one machine configures the fleet), and
the duty table (which job classes route where, with a live per-duty placement
policy the panel edits and the mesh gossips last-writer-wins).

Everything data-dependent rebuilds in place on ``store.mesh_changed`` (the same
``_rebuild_* + _clear_layout`` idiom the Panel uses), so the 2s poll never tears
down the whole column — only the parts whose data actually moved. Reads only;
the one write path is the inline editors, which call the ``store.mesh_*``
wrappers (those run the control-socket calls off the UI thread).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import core
from .mesh import config as mesh_config
from .mesh import statefile
from .mesh.config import PlacementOverrides
from .store import Store
from .widgets import ElidedLabel, tint_bg

# Link-state colours (shared with the wire graph + the node badges). Mirrors the
# duty/token palette in core/mesh.json so the whole feature reads as one thing.
_LINK_COLOR = {"up": "#34C759", "stale": "#FF9500", "down": "#FF3B30"}
_TOKEN_EMOJI = {t["id"]: t["emoji"] for t in core.mesh()["tokens"]}
_TOKEN_ORDER = [t["id"] for t in core.mesh()["tokens"]]


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _platform_meta(platform_id: str) -> tuple[str, str]:
    """(emoji, colorHex) for a platform id, from the shared model."""
    for p in core.mesh()["platforms"]:
        if p["id"] == platform_id:
            return p["emoji"], p["colorHex"]
    return "🖥️", "#8E8E93"


# MARK: - wire graph


class TopologyGraph(QWidget):
    """A compact node-link diagram: self centred, peers on a ring, links coloured
    by state; peer↔peer edges (from each peer's ``sees`` list) drawn thin/gray
    when both ends agree they see each other. Purely presentational — painted
    from the snapshot the MeshView hands it via :meth:`set_snapshot`."""

    def __init__(self) -> None:
        super().__init__()
        # Tall enough that the ring radius leaves room for a node disc AND its
        # name label without either colliding with the self disc at the centre.
        self.setFixedHeight(150)
        self._self: dict = {}
        self._peers: list[dict] = []

    def set_snapshot(self, self_node: dict, peers: list[dict]) -> None:
        self._self = self_node or {}
        self._peers = peers or []
        self.update()

    def _node_points(self) -> tuple[tuple[float, float], list[tuple[float, float]]]:
        """Self at the centre; peers on a ring around it (a single point when
        there are no peers)."""
        import math

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        # Ring radius: bounded by width, but also by height minus room for the
        # node disc + its name label, so ring nodes never clip the widget edge.
        radius = min(w * 0.33, (h / 2.0) - 30)
        peers = self._peers
        pts: list[tuple[float, float]] = []
        n = len(peers)
        for i in range(n):
            # Start at the top and go clockwise; -90° puts the first peer up top.
            ang = -math.pi / 2 + (2 * math.pi * i / max(n, 1))
            pts.append((cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
        return (cx, cy), pts

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._self:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        (cx, cy), pts = self._node_points()
        self_id = self._self.get("id")
        id_to_pt = {self_id: (cx, cy)}
        for peer, pt in zip(self._peers, pts):
            id_to_pt[peer.get("id")] = pt

        # peer↔peer edges first (behind), thin + gray when both agree they see
        # each other; a lone-direction sighting is dimmer still.
        seen_pairs: set[frozenset] = set()
        for peer in self._peers:
            a = peer.get("id")
            for other_id in peer.get("sees", []):
                if other_id == self_id or other_id not in id_to_pt:
                    continue
                pair = frozenset((a, other_id))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                other = next((p for p in self._peers if p.get("id") == other_id), None)
                mutual = other is not None and a in (other.get("sees") or [])
                pen = QPen(QColor(150, 150, 150, 150 if mutual else 70))
                pen.setWidthF(1.0)
                painter.setPen(pen)
                ax, ay = id_to_pt[a]
                bx, by = id_to_pt[other_id]
                painter.drawLine(int(ax), int(ay), int(bx), int(by))

        # self→peer links, coloured by link state.
        for peer, (px, py) in zip(self._peers, pts):
            color = _LINK_COLOR.get(peer.get("link"), "#8E8E93")
            pen = QPen(QColor(color))
            pen.setWidthF(2.0 if peer.get("link") == "up" else 1.6)
            if peer.get("link") == "down":
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(cx), int(cy), int(px), int(py))

        # nodes on top: self highlighted, peers tinted by platform.
        self._draw_node(painter, cx, cy, self._self, is_self=True)
        for peer, (px, py) in zip(self._peers, pts):
            self._draw_node(painter, px, py, peer, is_self=False)
        painter.end()

    def _draw_node(self, painter: QPainter, x: float, y: float, node: dict,
                   *, is_self: bool) -> None:
        emoji, color = _platform_meta(node.get("platform", ""))
        r = 15 if is_self else 12
        fill = QColor(color)
        if not is_self:
            fill.setAlpha(210)
        painter.setBrush(fill)
        pen = QPen(QColor("white") if is_self else QColor(0, 0, 0, 90))
        pen.setWidthF(2.0 if is_self else 1.0)
        painter.setPen(pen)
        painter.drawEllipse(int(x - r), int(y - r), int(r * 2), int(r * 2))

        # platform glyph inside the disc
        painter.setPen(QColor("white"))
        f = painter.font()
        f.setPixelSize(int(r * 0.95))
        painter.setFont(f)
        painter.drawText(int(x - r), int(y - r), int(r * 2), int(r * 2),
                         int(Qt.AlignmentFlag.AlignCenter), emoji)

        # Name label, elided to a sane width. Placed on the OUTSIDE of the disc
        # relative to centre — above the disc for nodes in the top half, below
        # otherwise — so a ring node's label never crosses the central self disc
        # (which a fixed "always below" placement does for any peer near the top).
        name = node.get("name", "?")
        f2 = painter.font()
        f2.setPixelSize(9)
        f2.setBold(is_self)
        painter.setFont(f2)
        fm = painter.fontMetrics()
        label = fm.elidedText(name, Qt.TextElideMode.ElideRight, 74)
        painter.setPen(QColor(230, 230, 230) if is_self else QColor(165, 165, 165))
        cy = self.height() / 2.0
        above = (not is_self) and y < cy - 1  # self label stays below its disc
        label_y = int(y - r - 2 - 12) if above else int(y + r + 2)
        painter.drawText(int(x - 40), label_y, 80, 12,
                         int(Qt.AlignmentFlag.AlignHCenter
                             | (Qt.AlignmentFlag.AlignBottom if above
                                else Qt.AlignmentFlag.AlignTop)),
                         label)


# MARK: - the column


class MeshView(QWidget):
    """The topology column shown at the far left of the panel."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("MeshView { background: transparent; }")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        col.addLayout(self._build_header())

        # Empty/off state host (shown instead of the graph/cards when there's no
        # live topology to render).
        self.state_host = QWidget()
        self.state_col = QVBoxLayout(self.state_host)
        self.state_col.setContentsMargins(7, 18, 7, 18)
        self.state_col.setSpacing(8)
        col.addWidget(self.state_host)

        # Live-topology host: wire graph + node cards + duties. Hidden while an
        # off/empty state is showing.
        self.live_host = QWidget()
        live = QVBoxLayout(self.live_host)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)

        self.graph = TopologyGraph()
        graph_wrap = QWidget()
        gw = QVBoxLayout(graph_wrap)
        gw.setContentsMargins(6, 6, 6, 6)
        gw.setSpacing(0)
        gw.addWidget(self.graph)
        graph_wrap.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 8px;"
        )
        live.addWidget(graph_wrap)

        # Node cards
        self.nodes_host = QWidget()
        self.nodes_col = QVBoxLayout(self.nodes_host)
        self.nodes_col.setContentsMargins(7, 7, 7, 7)
        self.nodes_col.setSpacing(6)
        self.nodes_host.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 8px;"
        )
        live.addWidget(self.nodes_host)

        # Duties
        self.duties_host = QWidget()
        self.duties_col = QVBoxLayout(self.duties_host)
        self.duties_col.setContentsMargins(7, 7, 7, 7)
        self.duties_col.setSpacing(6)
        self.duties_host.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 8px;"
        )
        live.addWidget(self.duties_host)

        col.addWidget(self.live_host)

        # Control-edit error (CtlError from an inline editor) — a small red line.
        self.err_line = QLabel("")
        self.err_line.setWordWrap(True)
        self.err_line.setStyleSheet("color: #FF3B30; font-size: 9px;")
        self.err_line.setVisible(False)
        col.addWidget(self.err_line)

        col.addStretch(1)

        store.mesh_changed.connect(self._rebuild)
        self._rebuild()

    # MARK: header

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        glyph = QLabel("🕸️")
        glyph.setStyleSheet("font-size: 11px;")
        row.addWidget(glyph)
        title = QLabel("MESH")
        title.setStyleSheet("color: palette(mid); font-weight: 700; font-size: 10px;")
        row.addWidget(title)
        self.node_count = QLabel("")
        self.node_count.setStyleSheet(
            "color: palette(mid); font-family: monospace; font-size: 10px;"
        )
        row.addWidget(self.node_count)
        row.addStretch(1)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: gray; font-size: 10px;")
        row.addWidget(self.status_dot)
        self.status_text = QLabel("")
        self.status_text.setStyleSheet("color: palette(mid); font-size: 9px;")
        row.addWidget(self.status_text)
        return row

    # MARK: rebuild

    def _rebuild(self) -> None:
        state = self.store.mesh_state
        running = statefile.node_running(state)

        self.err_line.setVisible(bool(self.store.mesh_error))
        if self.store.mesh_error:
            self.err_line.setText("⚠ " + self.store.mesh_error)

        # Off / empty / dead states — no live topology to render.
        if not self.store.mesh_enabled:
            self._show_state("🕸️", "Mesh is off",
                             "Enable it in ⚙ Settings to coordinate duties with "
                             "other machines on this LAN.", None)
            self._set_status("gray", "off")
            self.node_count.setText("")
            return
        if state is None:
            self._show_state("⏳", "Starting mesh node…",
                             "Discovering peers on the LAN.", None)
            self._set_status("#FF9500", "starting")
            self.node_count.setText("")
            return
        if not running:
            self._show_state("🛑", "Mesh node not running.",
                             "The node process is gone. Start it to rejoin the mesh.",
                             "Start")
            self._set_status("#FF3B30", "node dead")
            self.node_count.setText("")
            return

        # Live: paint the graph, cards, and duties.
        self.state_host.setVisible(False)
        self.live_host.setVisible(True)

        self_node = state.get("self") or {}
        peers = state.get("peers") or []
        self.graph.set_snapshot(self_node, peers)
        self.node_count.setText(str(1 + len(peers)))
        self._set_status("#34C759", "live")

        self._rebuild_nodes(self_node, peers)
        self._rebuild_duties(state, self_node, peers)

    def _set_status(self, color: str, text: str) -> None:
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self.status_text.setText(text)

    def _show_state(self, glyph: str, title: str, detail: str,
                    button: str | None) -> None:
        self.live_host.setVisible(False)
        self.state_host.setVisible(True)
        _clear_layout(self.state_col)

        g = QLabel(glyph)
        g.setAlignment(Qt.AlignmentFlag.AlignCenter)
        g.setStyleSheet("font-size: 26px;")
        self.state_col.addWidget(g)
        t = QLabel(title)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 12px; font-weight: 600;")
        self.state_col.addWidget(t)
        d = QLabel(detail)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        d.setStyleSheet("color: palette(mid); font-size: 10px;")
        self.state_col.addWidget(d)
        if button:
            btn = QPushButton(button)
            btn.setStyleSheet("font-weight: 700;")
            btn.clicked.connect(self.store.ensure_mesh_running_async)
            self.state_col.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)

    # MARK: node cards

    def _rebuild_nodes(self, self_node: dict, peers: list[dict]) -> None:
        _clear_layout(self.nodes_col)
        head = QLabel("NODES")
        head.setStyleSheet(
            "color: palette(mid); font-weight: 700; font-size: 9px;"
        )
        self.nodes_col.addWidget(head)

        # self first, then peers by name.
        self.nodes_col.addWidget(self._node_card(self_node, None))
        for peer in sorted(peers, key=lambda p: (p.get("name") or "").lower()):
            self.nodes_col.addWidget(self._node_card(peer, peer))

    def _node_card(self, node: dict, peer: dict | None) -> QWidget:
        """One node row: platform chip + name + self/link badge + addr, and inline
        tier / token editors. ``peer`` is None for self, else the peer dict (its
        link/addr live there)."""
        node_id = node.get("id", "")
        emoji, color = _platform_meta(node.get("platform", ""))

        card = QWidget()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        card.setStyleSheet(
            "background-color: rgba(128,128,128,0.06); border-radius: 6px;"
        )

        # Top row: chip · name · badge
        top = QHBoxLayout()
        top.setSpacing(6)
        chip = QLabel(emoji)
        chip.setFixedSize(20, 20)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(
            f"background-color: {tint_bg(color, 0.9)}; border-radius: 5px; font-size: 11px;"
        )
        top.addWidget(chip)
        name = ElidedLabel(node.get("name", "?"), 11, "#d8dbde")
        top.addWidget(name, 1)

        if peer is None:
            badge = QLabel("self")
            badge.setStyleSheet(
                "color: #34C759; font-weight: 700; font-size: 8px;"
                f" background-color: {tint_bg('#34C759', 0.15)}; border-radius: 6px;"
                " padding: 1px 5px;"
            )
        else:
            link = peer.get("link", "down")
            lcolor = _LINK_COLOR.get(link, "#8E8E93")
            ago = peer.get("lastSeenSecsAgo")
            ago_txt = f" {int(ago)}s" if isinstance(ago, (int, float)) else ""
            badge = QLabel(f"{link}{ago_txt}")
            badge.setStyleSheet(
                f"color: {lcolor}; font-weight: 700; font-size: 8px;"
                f" background-color: {tint_bg(lcolor, 0.15)}; border-radius: 6px;"
                " padding: 1px 5px;"
            )
        top.addWidget(badge)
        outer.addLayout(top)

        # Addr line (peers only — self has no remote addr)
        if peer is not None and peer.get("addr"):
            addr = QLabel(str(peer["addr"]))
            addr.setStyleSheet(
                "color: palette(mid); font-family: monospace; font-size: 9px;"
            )
            outer.addWidget(addr)

        # Editors row: tier stepper + token selector. Both edit ANY node.
        editors = QHBoxLayout()
        editors.setSpacing(8)
        editors.addLayout(self._tier_editor(node_id, int(node.get("tier", 3))))
        editors.addWidget(self._token_editor(node_id, node.get("tokens", "ok")))
        editors.addStretch(1)
        outer.addLayout(editors)
        return card

    def _tier_editor(self, node_id: str, tier: int) -> QHBoxLayout:
        lo, hi, _ = mesh_config.tier_bounds()
        row = QHBoxLayout()
        row.setSpacing(3)

        def step_btn(glyph: str) -> QToolButton:
            b = QToolButton()
            b.setText(glyph)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton { border: none; color: palette(mid); font-size: 12px;"
                " font-weight: 700; padding: 0 3px; }"
                "QToolButton:hover { color: palette(text); }"
                "QToolButton:disabled { color: rgba(128,128,128,0.35); }"
            )
            return b

        minus = step_btn("−")
        minus.setEnabled(tier > lo)
        minus.clicked.connect(
            lambda: self.store.mesh_set_attr(node_id, {"tier": max(lo, tier - 1)})
        )
        row.addWidget(minus)

        label = QLabel(f"tier {tier}")
        label.setStyleSheet("font-size: 10px; font-family: monospace;")
        label.setToolTip("Machine strength (1 = strongest)")
        row.addWidget(label)

        plus = step_btn("+")
        plus.setEnabled(tier < hi)
        plus.clicked.connect(
            lambda: self.store.mesh_set_attr(node_id, {"tier": min(hi, tier + 1)})
        )
        row.addWidget(plus)
        return row

    def _token_editor(self, node_id: str, tokens: str) -> QComboBox:
        combo = QComboBox()
        combo.setStyleSheet("font-size: 10px;")
        for tid in _TOKEN_ORDER:
            combo.addItem(f"{_TOKEN_EMOJI[tid]} {tid}", tid)
        idx = combo.findData(tokens)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.setToolTip("Token budget state — the mesh routes around 'out' nodes")
        # Only fire on a user-driven change (not the programmatic setCurrentIndex).
        combo.activated.connect(
            lambda _i, c=combo: self.store.mesh_set_attr(
                node_id, {"tokens": c.currentData()}
            )
        )
        return combo

    # MARK: duties

    def _rebuild_duties(self, state: dict, self_node: dict,
                        peers: list[dict]) -> None:
        _clear_layout(self.duties_col)
        head = QLabel("DUTIES")
        head.setStyleSheet(
            "color: palette(mid); font-weight: 700; font-size: 9px;"
        )
        self.duties_col.addWidget(head)

        overrides = PlacementOverrides.from_dict(state.get("overrides"))
        assignments = state.get("assignments") or {}
        # id -> name, for turning assigned ids into readable names.
        id_to_name = {self_node.get("id"): self_node.get("name", "self")}
        for p in peers:
            id_to_name[p.get("id")] = p.get("name", p.get("id", "?")[:6])

        for duty in core.mesh()["duties"]:
            self.duties_col.addWidget(
                self._duty_card(duty, assignments.get(duty["id"], {}),
                                overrides, id_to_name)
            )

    def _duty_card(self, duty: dict, assignment: dict,
                   overrides: PlacementOverrides, id_to_name: dict) -> QWidget:
        duty_id = duty["id"]
        placement = mesh_config.placement_for(duty_id, overrides)

        card = QWidget()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        card.setStyleSheet(
            "background-color: rgba(128,128,128,0.06); border-radius: 6px;"
        )

        # Title row: emoji + title
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        em = QLabel(duty["emoji"])
        em.setStyleSheet("font-size: 12px;")
        title_row.addWidget(em)
        t = QLabel(duty["title"])
        t.setStyleSheet("font-size: 11px; font-weight: 600;")
        title_row.addWidget(t)
        title_row.addStretch(1)
        outer.addLayout(title_row)

        # Assigned → names, or empty / shortfall warning.
        assigned = assignment.get("assigned", [])
        shortfall = assignment.get("shortfall", [])
        if assigned:
            names = ", ".join(id_to_name.get(nid, nid[:6]) for nid in assigned)
            arrow = ElidedLabel(f"→ {names}", 10, "#a5a5a5")
            outer.addWidget(arrow)
        elif not shortfall:
            nobody = QLabel("∅ nobody")
            nobody.setStyleSheet("color: palette(mid); font-size: 10px;")
            outer.addWidget(nobody)
        if shortfall:
            miss = " · ".join(
                f"⚠ missing {m.get('missing', 1)}×{m.get('platform', '?')}"
                for m in shortfall
            )
            warn = QLabel(miss)
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #FF9500; font-size: 9px; font-weight: 600;")
            outer.addWidget(warn)

        # Spread (static — spread editing is out of scope).
        if placement.spread:
            parts = []
            for plat, cnt in placement.spread:
                pemoji, _ = _platform_meta(plat)
                parts.append(f"{cnt}×{pemoji}")
            spread = QLabel("spread: " + "+".join(parts))
            spread.setStyleSheet(
                "color: palette(mid); font-family: monospace; font-size: 9px;"
            )
            outer.addWidget(spread)

        # Policy editors: strategy combo + token-aware checkbox.
        policy = QHBoxLayout()
        policy.setSpacing(6)
        strat = QComboBox()
        strat.setStyleSheet("font-size: 9px;")
        for s in core.mesh()["strategies"]:
            strat.addItem(s["title"], s["id"])
            strat.setItemData(strat.count() - 1, s["detail"], Qt.ItemDataRole.ToolTipRole)
        sidx = strat.findData(placement.strategy)
        if sidx >= 0:
            strat.setCurrentIndex(sidx)
        strat.activated.connect(
            lambda _i, c=strat: self._edit_placement(
                duty_id, placement, strategy=c.currentData()
            )
        )
        policy.addWidget(strat, 1)

        tok_aware = QCheckBox("token-aware")
        tok_aware.setStyleSheet("font-size: 9px;")
        tok_aware.setChecked(placement.token_aware)
        tok_aware.setToolTip("Skip nodes that are out of tokens when routing")
        tok_aware.toggled.connect(
            lambda on: self._edit_placement(duty_id, placement, token_aware=on)
        )
        policy.addWidget(tok_aware)
        outer.addLayout(policy)
        return card

    def _edit_placement(self, duty_id: str, current, *, strategy: str | None = None,
                        token_aware: bool | None = None) -> None:
        """Push one placement edit to the mesh (LWW-gossiped). Spread is preserved
        as-is; only the strategy / token-awareness the panel exposes can change."""
        new = current.to_dict()
        if strategy is not None:
            new["strategy"] = strategy
        if token_aware is not None:
            new["tokenAware"] = token_aware
        self.store.mesh_set_overrides(duty_id, new)
