"""The Mesh management screen — read the LAN, configure the whole mesh.

The Linux face of Diplomat Mesh (see ``assets/mesh.json`` for the model and
``szpontnet`` for the node). One of the panel's three screens (Actions ·
Mesh · Settings), it renders the local node's public topology snapshot
(``~/.diplomat/mesh/state.json``): a compact wire graph of self + peers,
one editable card per node (machine strength in words + an auto-measured token
budget + the transport that edge runs over + a Personal/Foreign trust toggle —
edits apply to *any* node, self or peer, forwarded over the mesh so one machine
configures the fleet), the WAN card (the mesh's preferred transport, this
machine's id on each transport it runs, and the paste box that links to a peer's
— a handshake that needs no LAN meeting), and the duty table (which job classes
route where, with a live per-duty placement policy the panel edits and the mesh
gossips last-writer-wins).

Everything data-dependent rebuilds in place on ``store.mesh_changed`` (the same
``_rebuild_* + _clear_layout`` idiom the Panel uses), so the 2s poll never tears
down the whole screen — only the parts whose data actually moved. Reads only;
the one write path is the inline editors, which call the ``store.mesh_*``
wrappers (those run the control-socket calls off the UI thread).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from diplomat_runtime import core
from . import glyphs
from szpontnet import config as mesh_config
from szpontnet import statefile
from szpontnet.config import PlacementOverrides
from .store import Store
from .widgets import ElidedLabel, GlyphLabel, IconChip, card_host, muted, tint_bg

# Link-state colours (shared with the wire graph + the node badges). Mirrors the
# duty/token palette in assets/mesh.json so the whole feature reads as one thing.
_LINK_COLOR = {"up": "#34C759", "stale": "#FF9500", "down": "#FF3B30"}
# Monochrome token glyph + colour, read straight from the shared model (like
# activity.py reads linuxGlyph) so the combo tints like the rest of the applet.
_TOKEN_GLYPH = {t["id"]: t.get("linuxGlyph", t["emoji"]) for t in core.mesh()["tokens"]}
_TOKEN_COLOR = {t["id"]: t["colorHex"] for t in core.mesh()["tokens"]}
_TOKEN_ORDER = [t["id"] for t in core.mesh()["tokens"]]
# Trust levels (personal/foreign) — the toggle vocabulary, from the shared model.
_TRUST_META = {t["id"]: t for t in core.mesh()["trust"]["levels"]}
# How an edge runs (lan / iroh / tor), from the shared model. ``_WAN_TRANSPORTS`` is
# the pickable subset — the LAN is where peers that share a network meet, never a
# choice — and its order is the one the preferred-transport toggle offers.
_TRANSPORT_META = {t["id"]: t for t in core.mesh()["transports"]}
_WAN_TRANSPORTS = [t["id"] for t in core.mesh()["transports"] if t.get("wan")]


def _fmt_dur(secs: float | None) -> str:
    """Compact human duration: 5s / 3m / 2h / 1d. Used for link uptime + 'seen'."""
    if secs is None:
        return "?"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _wan_transport_state(state: dict, transport_id: str) -> dict:
    """One WAN transport's block in the snapshot (``enabled``/``ready``/``address``),
    or an empty dict on a node that reports none — which is how every reader here
    treats "this machine does not run it"."""
    block = ((state.get("wan") or {}).get("transports") or {}).get(transport_id)
    return block if isinstance(block, dict) else {}


def _transport_meta(transport_id: str) -> tuple[str, str, str]:
    """(monochrome glyph, colorHex, title) for a transport id, from the shared model.
    An unknown id — a peer running a transport this build has no vocabulary for —
    renders as a neutral link rather than vanishing."""
    meta = _TRANSPORT_META.get(transport_id)
    if meta is None:
        return "→", "#8E8E93", transport_id or "?"
    return meta.get("linuxGlyph", meta["emoji"]), meta["colorHex"], meta["title"]


def _platform_meta(platform_id: str) -> tuple[str, str]:
    """(monochrome glyph, colorHex) for a platform id, from the shared model.

    Reads the additive ``linuxGlyph`` field (like activity.py) so the mesh column
    renders flat tinted glyphs instead of colour-emoji; falls back to a neutral
    node glyph for an unknown platform."""
    for p in core.mesh()["platforms"]:
        if p["id"] == platform_id:
            return p.get("linuxGlyph", p["emoji"]), p["colorHex"]
    return "⬢", "#8E8E93"


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
        # (A bit taller than the old side-column graph — the screen has room.)
        self.setFixedHeight(190)
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
        glyph, color = _platform_meta(node.get("platform", ""))
        r = 15 if is_self else 12
        fill = QColor(color)
        if not is_self:
            fill.setAlpha(210)
        painter.setBrush(fill)
        pen = QPen(QColor("white") if is_self else QColor(0, 0, 0, 90))
        pen.setWidthF(2.0 if is_self else 1.0)
        painter.setPen(pen)
        painter.drawEllipse(int(x - r), int(y - r), int(r * 2), int(r * 2))

        # monochrome platform glyph inside the disc, white on the tint
        painter.setPen(QColor("white"))
        f = painter.font()
        f.setPixelSize(int(r * 0.95))
        painter.setFont(f)
        painter.drawText(int(x - r), int(y - r), int(r * 2), int(r * 2),
                         int(Qt.AlignmentFlag.AlignCenter), glyph)

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


# MARK: - the screen


class MeshView(QWidget):
    """The Mesh management screen (the panel's third screen, beside Actions and
    Settings). Emits ``done`` when the user is finished, like SettingsView."""

    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("MeshView { background: transparent; }")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        col.addLayout(self._build_header())

        # Major-issue banner for a node whose every beacon send fails (state.json
        # `beaconBlocked`): the OS/firewall is denying LAN sends, so this machine is
        # invisible to its peers and a dropped link won't re-form. Without this the
        # mesh just looks inexplicably empty.
        self.blocked_banner = QLabel(
            "⚠ DEVICE IS NOT DISCOVERABLE — every discovery beacon send is failing "
            "(OS privacy gate or firewall). Peers cannot find this machine."
        )
        self.blocked_banner.setWordWrap(True)
        self.blocked_banner.setStyleSheet(
            "color: #FF3B30; font-size: 10px; font-weight: 800;"
            " background-color: rgba(255,59,48,0.12); border-radius: 6px;"
            " border: 1px solid rgba(255,59,48,0.45); padding: 5px 8px;"
        )
        self.blocked_banner.setVisible(False)
        col.addWidget(self.blocked_banner)

        # Animated "scanning the LAN" banner — shown while the node is starting or
        # still discovering peers, so establishing the mesh isn't a silent ~20s wait.
        self.scan_banner = QLabel("")
        self.scan_banner.setStyleSheet(
            "color: #30B0C7; font-size: 10px; font-weight: 600;"
            " background-color: rgba(48,176,199,0.10); border-radius: 6px;"
            " padding: 4px 8px;"
        )
        self.scan_banner.setVisible(False)
        col.addWidget(self.scan_banner)
        self._scan_phase = 0
        self._scan_base = ""
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(500)
        self._scan_timer.timeout.connect(self._tick_scan)

        # Empty/off state host (shown instead of the graph/cards when there's no
        # live topology to render).
        self.state_host = QWidget()
        self.state_col = QVBoxLayout(self.state_host)
        self.state_col.setContentsMargins(7, 18, 7, 18)
        self.state_col.setSpacing(8)
        col.addWidget(self.state_host)

        # Live-topology host: the wire graph up top, node cards and duties as
        # side-by-side columns below (the screen is wide; a single stacked
        # column would stretch every card across the whole panel).
        self.live_host = QWidget()
        live = QVBoxLayout(self.live_host)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)

        self.graph = TopologyGraph()
        graph_wrap, gw = card_host(padding=6, spacing=0)
        gw.addWidget(self.graph)
        live.addWidget(graph_wrap)

        # Reaching machines off this network: the mesh's preferred WAN transport, this
        # node's id on each transport it runs, and the paste box that links to a peer's.
        self.wan_host, self.wan_col = card_host()
        self._build_wan_card()
        live.addWidget(self.wan_host)

        columns = QHBoxLayout()
        columns.setSpacing(12)

        # Node cards
        self.nodes_host, self.nodes_col = card_host()
        self.nodes_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        columns.addWidget(self.nodes_host, 1, Qt.AlignmentFlag.AlignTop)

        # Duties
        self.duties_host, self.duties_col = card_host()
        self.duties_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        columns.addWidget(self.duties_host, 1, Qt.AlignmentFlag.AlignTop)

        live.addLayout(columns)
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
        # Screen header, matching SettingsView: title on the left, Done on the
        # right, with the live node count + status between them.
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_MESH, 16, glyphs.MUTED, font_px=14))
        title = QLabel("Mesh")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        self.node_count = QLabel("")
        self.node_count.setStyleSheet(
            muted(10, mono=True)
        )
        row.addWidget(self.node_count)
        row.addStretch(1)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: gray; font-size: 10px;")
        row.addWidget(self.status_dot)
        self.status_text = QLabel("")
        self.status_text.setStyleSheet(muted(9))
        row.addWidget(self.status_text)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    # MARK: rebuild

    def _rebuild(self) -> None:
        state = self.store.mesh_state
        running = statefile.node_running(state)

        self.err_line.setVisible(bool(self.store.mesh_error))
        if self.store.mesh_error:
            self.err_line.setText("⚠ " + self.store.mesh_error)

        # The blocked banner only makes sense over a live node (a dead/off node is
        # undiscoverable for a plainer reason the state hosts already explain). Its
        # text tracks the node's own diagnosis (`beaconBlockReason`) so it matches the
        # activity log instead of always blaming the OS privacy gate.
        blocked = running and bool((state or {}).get("beaconBlocked"))
        self.blocked_banner.setVisible(blocked)
        if blocked:
            if (state or {}).get("beaconBlockReason") == "network-down":
                self.blocked_banner.setText(
                    "⚠ DEVICE IS NOT DISCOVERABLE — no usable network (even a loopback "
                    "send fails). Check this machine's connection.")
            else:
                self.blocked_banner.setText(
                    "⚠ DEVICE IS NOT DISCOVERABLE — the OS or a firewall is blocking "
                    "this node's LAN sends, so peers can't find it. Check the host "
                    "firewall isn't dropping multicast/broadcast on the mesh port.")

        # Off / empty / dead states — no live topology to render.
        if not self.store.mesh_enabled:
            self._set_scan(None)
            self._show_state(glyphs.G_MESH, "Mesh is off",
                             "Enable it in ⚙ Settings to coordinate duties with "
                             "other machines on this LAN.", None)
            self._set_status("gray", "off")
            self.node_count.setText("")
            return
        if state is None:
            self._show_state("⧗", "Starting mesh node…",
                             "Binding the discovery socket and beaconing.", None)
            self._set_status("#FF9500", "starting")
            self._set_scan("Starting the mesh node")
            self.node_count.setText("")
            return
        if not running:
            self._set_scan(None)
            self._show_state("⊘", "Mesh node not running.",
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

        # Still finding the fleet? Keep an animated, elapsed-timed banner up so a
        # slow first link reads as "scanning", not a frozen empty graph.
        linking = int(state.get("linking") or 0)
        if not peers or linking:
            elapsed = self_node.get("uptimeSecs")
            elapsed_txt = f" ({_fmt_dur(elapsed)})" if isinstance(elapsed, (int, float)) else ""
            if linking:
                self._set_scan(f"Linking to {linking} machine{'s' if linking != 1 else ''}")
            else:
                self._set_scan(f"Scanning the LAN for machines{elapsed_txt}")
        else:
            self._set_scan(None)

        # Before the node cards: each peer's edge row asks what this machine can
        # reach over (``_wan_ready``), which this settles from the same snapshot.
        self._refresh_wan(state)
        self._rebuild_nodes(self_node, peers)
        self._rebuild_duties(state, self_node, peers)

    def _set_status(self, color: str, text: str) -> None:
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self.status_text.setText(text)

    def _set_scan(self, base: str | None) -> None:
        """Show/hide the animated scanning banner. ``base`` is the message stem; the
        timer appends cycling dots so it visibly pulses between 2s snapshots."""
        if base is None:
            self._scan_timer.stop()
            self.scan_banner.setVisible(False)
            return
        self._scan_base = base
        self.scan_banner.setVisible(True)
        if not self._scan_timer.isActive():
            self._scan_phase = 0
            self._scan_timer.start()
        self._render_scan()

    def _tick_scan(self) -> None:
        self._scan_phase = (self._scan_phase + 1) % 4
        self._render_scan()

    def _render_scan(self) -> None:
        self.scan_banner.setText(f"⧗ {self._scan_base}{'.' * self._scan_phase}")

    def _show_state(self, glyph: str, title: str, detail: str,
                    button: str | None) -> None:
        self.live_host.setVisible(False)
        self.state_host.setVisible(True)
        _clear_layout(self.state_col)

        g = GlyphLabel(glyph, 34, glyphs.MUTED, font_px=28)
        self.state_col.addWidget(g, 0, Qt.AlignmentFlag.AlignCenter)
        t = QLabel(title)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 12px; font-weight: 600;")
        self.state_col.addWidget(t)
        d = QLabel(detail)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        d.setStyleSheet(muted(10))
        self.state_col.addWidget(d)
        if button:
            btn = QPushButton(button)
            btn.setStyleSheet("font-weight: 700;")
            btn.clicked.connect(self.store.ensure_mesh_running_async)
            self.state_col.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)

    # MARK: WAN reachability

    def _build_wan_card(self) -> None:
        """Build the WAN card ONCE, in __init__.

        Everything else on this screen is rebuilt from the snapshot; this card is
        refreshed in place (:meth:`_refresh_wan`) because it holds a text field. A
        2s poll that tore down and recreated a QLineEdit would eat the id the
        operator is halfway through pasting into it."""
        head = QLabel("REACHING MACHINES OFF THIS NETWORK")
        head.setStyleSheet(muted(9, bold=True))
        self.wan_col.addWidget(head)

        # Preferred transport — a segmented pick, gossiped mesh-wide.
        pref_row = QHBoxLayout()
        pref_row.setSpacing(4)
        lbl = QLabel("preferred")
        lbl.setStyleSheet(muted(9))
        pref_row.addWidget(lbl)
        self.pref_buttons: dict[str, QToolButton] = {}
        for tid in _WAN_TRANSPORTS:
            glyph, color, title = _transport_meta(tid)
            btn = QToolButton()
            btn.setText(f"{glyph} {title}")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(
                f"{_TRANSPORT_META[tid].get('detail', '')}\n\n"
                "The mesh's preferred WAN transport, gossiped to every node. An edge "
                "uses the most-preferred transport BOTH its ends run, so a peer that "
                "runs only the other one is still reached over that one. Takes effect "
                "on the next reconnect — a live link is never moved."
            )
            btn.clicked.connect(lambda _c=False, t=tid: self.store.mesh_set_wan(t))
            self.pref_buttons[tid] = btn
            pref_row.addWidget(btn)
        pref_row.addStretch(1)
        self.wan_col.addLayout(pref_row)

        # My id, one row per WAN transport this machine runs.
        self.id_rows: dict[str, tuple[QWidget, QLabel, QLabel]] = {}
        for tid in _WAN_TRANSPORTS:
            glyph, color, title = _transport_meta(tid)
            row_host = QWidget()
            row = QHBoxLayout(row_host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            cap = QLabel("my id")
            cap.setStyleSheet(muted(9))
            row.addWidget(cap)
            mark = QLabel(glyph)
            mark.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 700;")
            row.addWidget(mark)
            value = ElidedLabel("", 9, "#d8dbde")
            row.addWidget(value, 1)
            copy = QToolButton()
            copy.setText("copy")
            copy.setCursor(Qt.CursorShape.PointingHandCursor)
            copy.setStyleSheet(
                "QToolButton { border: none; font-size: 9px; padding: 1px 6px;"
                " border-radius: 6px; color: palette(mid); }")
            copy.setToolTip(f"Copy this machine's {title} id — paste it into another "
                            "machine's Mesh screen to link the two.")
            copy.clicked.connect(lambda _c=False, t=tid: self._copy_my_id(t))
            row.addWidget(copy)
            self.id_rows[tid] = (row_host, value, copy)
            self.wan_col.addWidget(row_host)
        # WAN transports with an address up right now — what a peer card checks before
        # calling a missing shared transport a problem (with none of our own, "no
        # shared transport" is this machine's LAN-only state, said once above).
        self._wan_ready: list[str] = []

        # The state line for a machine with no WAN transport up (nothing to copy).
        self.wan_note = QLabel("")
        self.wan_note.setWordWrap(True)
        self.wan_note.setStyleSheet(muted(9))
        self.wan_col.addWidget(self.wan_note)

        # Connect to a peer's id — the manual introduction, no LAN meeting needed.
        conn_row = QHBoxLayout()
        conn_row.setSpacing(4)
        cap = QLabel("connect to id")
        cap.setStyleSheet(muted(9))
        conn_row.addWidget(cap)
        self.connect_field = QLineEdit()
        self.connect_field.setPlaceholderText(
            "paste another machine's id — an iroh endpoint id or a .onion")
        self.connect_field.setStyleSheet("font-size: 10px;")
        self.connect_field.returnPressed.connect(self._connect_clicked)
        conn_row.addWidget(self.connect_field, 1)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setStyleSheet("font-weight: 700;")
        self.connect_btn.setToolTip(
            "Dial that machine now, over whichever transport its id names — it works "
            "even if the two were never on a LAN together. From then on it is an "
            "ordinary mesh member and reconnects on its own.")
        self.connect_btn.clicked.connect(self._connect_clicked)
        conn_row.addWidget(self.connect_btn)
        self.wan_col.addLayout(conn_row)

    def _copy_my_id(self, transport: str) -> None:
        """Put this machine's id on that transport on the clipboard (the half of a
        manual handshake this machine owns)."""
        address = _wan_transport_state(self.store.mesh_state or {},
                                       transport).get("address")
        if address:
            QGuiApplication.clipboard().setText(str(address))

    def _connect_clicked(self) -> None:
        address = self.connect_field.text().strip()
        if not address:
            return
        # Cleared on submit, not on success: the dial runs in the background and its
        # outcome arrives as a peer appearing (or an error on the line below), so
        # there is nothing here to wait for. A stale id left in the box would just
        # get re-sent by the next Return.
        self.connect_field.clear()
        self.store.mesh_connect(address)

    def _refresh_wan(self, state: dict) -> None:
        """Update the WAN card in place from the snapshot."""
        preferred = (state.get("wan") or {}).get("preferred", "")
        for tid, btn in self.pref_buttons.items():
            _glyph, color, _title = _transport_meta(tid)
            active = tid == preferred
            btn.setChecked(active)
            btn.setStyleSheet(
                "QToolButton { border: none; font-size: 9px; font-weight: 700;"
                f" padding: 1px 6px; border-radius: 6px;"
                f" color: {color if active else 'palette(mid)'};"
                f" background-color: {tint_bg(color, 0.18) if active else 'transparent'}; }}"
            )

        ready = []
        for tid, (row_host, value, copy) in self.id_rows.items():
            block = _wan_transport_state(state, tid)
            address = block.get("address")
            # A transport the operator turned off has no row. One that is on without an
            # address yet says so rather than showing blank — Tor's bootstrap runs into
            # minutes, and where the library or binary is absent it never arrives.
            row_host.setVisible(bool(block.get("enabled")))
            value.set_text(str(address) if address else "coming up…")
            copy.setEnabled(bool(address))
            if address:
                ready.append(tid)
        self._wan_ready = ready

        if ready:
            self.wan_note.setVisible(False)
        else:
            self.wan_note.setVisible(True)
            self.wan_note.setText(
                "No WAN transport is up on this machine — it can only reach peers on "
                "this network. Install the szpontnet[wan] extra for iroh, or a tor "
                "binary, and restart the node."
            )
        # Connecting needs a transport of our own to dial from.
        self.connect_field.setEnabled(bool(ready))
        self.connect_btn.setEnabled(bool(ready))

    # MARK: node cards

    def _rebuild_nodes(self, self_node: dict, peers: list[dict]) -> None:
        _clear_layout(self.nodes_col)
        head = QLabel("NODES")
        head.setStyleSheet(
            muted(9, bold=True)
        )
        self.nodes_col.addWidget(head)

        # self first, then peers by name.
        self.nodes_col.addWidget(self._node_card(self_node, None))
        for peer in sorted(peers, key=lambda p: (p.get("name") or "").lower()):
            self.nodes_col.addWidget(self._node_card(peer, peer))

        # Zero-trust by default: a new device is Foreign until promoted. Spell out
        # the current default so the trust toggles read as intentional, not inert.
        state = self.store.mesh_state or {}
        default_trust = state.get("defaultTrust", "foreign")
        trusted = state.get("trusted") or []
        if peers and not trusted and default_trust == "foreign":
            hint = QLabel("Zero-trust default — a new device is Foreign until you mark it "
                          "Personal. Foreign devices' requests are declined (or run confined).")
            hint.setWordWrap(True)
            hint.setStyleSheet(muted(8))
            self.nodes_col.addWidget(hint)

    def _node_card(self, node: dict, peer: dict | None) -> QWidget:
        """One node row: platform chip + name + self/uptime badge + addr, and inline
        strength / token editors (plus a trust toggle for peers). ``peer`` is None
        for self, else the peer dict (its link/addr/trust live there)."""
        node_id = node.get("id", "")
        glyph, color = _platform_meta(node.get("platform", ""))

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
        top.addWidget(IconChip(glyph, color, 20),
                      0, Qt.AlignmentFlag.AlignVCenter)
        name = ElidedLabel(node.get("name", "?"), 11, "#d8dbde")
        top.addWidget(name, 1)
        top.addWidget(self._status_badge(peer))
        outer.addLayout(top)

        # Addr line (peers only — self has no remote addr)
        if peer is not None and peer.get("addr"):
            addr = QLabel(str(peer["addr"]))
            addr.setStyleSheet(
                muted(9, mono=True)
            )
            outer.addWidget(addr)

        # Editors row: strength + token, both auto by default, both edit ANY node.
        editors = QHBoxLayout()
        editors.setSpacing(8)
        editors.addWidget(self._strength_editor(
            node_id, int(node.get("tier", 3)), bool(node.get("strengthAuto", True))))
        editors.addWidget(self._token_editor(
            node_id, node.get("tokens", "ok"), bool(node.get("tokensAuto", True))))
        editors.addStretch(1)
        outer.addLayout(editors)

        # Read-only quota indicator, deliberately separate from the editor above.
        outer.addLayout(self._quota_row(node))

        # How this edge runs, and trust in it (peers only — self is neither an edge
        # nor a trust decision).
        if peer is not None:
            outer.addLayout(self._edge_row(peer))
            outer.addLayout(self._trust_toggle(peer))
        return card

    def _edge_row(self, peer: dict) -> QHBoxLayout:
        """One edge, one transport. Shows the transport the live link actually runs
        over, and — for a peer we currently reach on this network — the WAN transport
        the two would re-form over once they part, since that is the part a LAN link
        hides until it is too late to fix."""
        row = QHBoxLayout()
        row.setSpacing(4)
        cap = QLabel("via")
        cap.setStyleSheet(muted(9))
        row.addWidget(cap)

        live = peer.get("transport", "lan")
        wan = peer.get("wan", "")
        linked = peer.get("link") in ("up", "stale")

        def mark(transport_id: str) -> QLabel:
            glyph, color, title = _transport_meta(transport_id)
            lbl = QLabel(f"{glyph} {title}")
            lbl.setStyleSheet(f"color: {color}; font-size: 9px; font-weight: 700;")
            return lbl

        if linked and live != "lan":
            chip = mark(live)
            chip.setToolTip(f"This link runs over {_transport_meta(live)[2]}.")
            row.addWidget(chip)
        elif linked:
            chip = mark("lan")
            chip.setToolTip("This link is a direct connection on this network.")
            row.addWidget(chip)
            row.addWidget(self._offlan_note(wan))
        else:
            row.addWidget(self._offlan_note(wan))
        row.addStretch(1)
        return row

    def _offlan_note(self, wan: str) -> QLabel:
        """How this pair reaches each other once they are not on one network: the
        agreed WAN transport, or the warning that they agree on none."""
        if wan:
            glyph, color, title = _transport_meta(wan)
            note = QLabel(f"→ {glyph} {title} off-LAN")
            note.setStyleSheet(f"color: {color}; font-size: 9px;")
            note.setToolTip(
                f"Off this network the two reconnect over {title} — the transport "
                "both machines run, ranked by the mesh's preference.")
            return note
        if not self._wan_ready:
            # This machine has no WAN transport at all, which the card above already
            # says once. Repeating it per peer would read as a per-peer fault.
            note = QLabel("→ this network only")
            note.setStyleSheet(muted(9))
            return note
        note = QLabel("⚠ no shared WAN transport")
        note.setStyleSheet("color: #FF9500; font-size: 9px; font-weight: 600;")
        note.setToolTip(
            "This machine and that one run no WAN transport in common, so the link "
            "cannot re-form once they leave this network. Run the same transport on "
            "both — or link them by hand with the id above.")
        return note

    def _status_badge(self, peer: dict | None) -> QLabel:
        """'self' for the local node; for a peer, the connection UPTIME while linked
        ('up 3m', counting up) or 'down · seen 2m ago' once the link is lost — so the
        number is a meaningful, increasing clock, not the old always-~0 'up 0s'."""
        if peer is None:
            b = QLabel("self")
            b.setStyleSheet(
                "color: #34C759; font-weight: 700; font-size: 8px;"
                f" background-color: {tint_bg('#34C759', 0.15)}; border-radius: 6px;"
                " padding: 1px 5px;"
            )
            return b
        link = peer.get("link", "down")
        lcolor = _LINK_COLOR.get(link, "#8E8E93")
        if link in ("up", "stale"):
            up = peer.get("uptimeSecs")
            text = f"{link} {_fmt_dur(up)}" if isinstance(up, (int, float)) else link
            tip = "Connected — time since the link came up."
        else:
            ago = peer.get("lastSeenSecsAgo")
            text = (f"down · seen {_fmt_dur(ago)} ago"
                    if isinstance(ago, (int, float)) else "down")
            tip = "Link lost — how long since this peer was last heard from."
        b = QLabel(text)
        b.setToolTip(tip)
        b.setStyleSheet(
            f"color: {lcolor}; font-weight: 700; font-size: 8px;"
            f" background-color: {tint_bg(lcolor, 0.15)}; border-radius: 6px;"
            " padding: 1px 5px;"
        )
        return b

    def _strength_editor(self, node_id: str, tier: int, auto: bool) -> QComboBox:
        """Machine-strength picker in plain words. 'Auto' (the default) tracks the
        hardware-detected tier; picking a word pins it. 1 = strongest."""
        lo, hi, _ = mesh_config.tier_bounds()
        combo = QComboBox()
        combo.setStyleSheet("font-size: 10px;")
        # Item 0: Auto, showing the currently-detected word so it's never a mystery.
        combo.addItem(f"Auto · {mesh_config.tier_label(tier)}", "auto")
        for t in range(lo, hi + 1):
            combo.addItem(mesh_config.tier_label(t), t)
        combo.setCurrentIndex(0 if auto else max(0, combo.findData(tier)))
        combo.setToolTip(
            "Machine strength — how much compute this node has, auto-detected from "
            "RAM/CPU/GPU (1 = strongest). 'weakest-first' routing keeps strong "
            "machines free; pick a word to pin it, or Auto to re-detect."
        )
        combo.activated.connect(
            lambda _i, c=combo: self._edit_strength(node_id, c.currentData())
        )
        return combo

    def _edit_strength(self, node_id: str, data) -> None:
        if data == "auto":
            self.store.mesh_set_attr(node_id, {"strengthAuto": True})
        else:
            self.store.mesh_set_attr(node_id, {"tier": int(data)})

    def _token_editor(self, node_id: str, tokens: str, auto: bool) -> QComboBox:
        """Token-budget *setting* only — the measurement lives in the quota row, so
        this combo never doubles as an indicator. 'Auto' (default) derives ok/low/out
        from the node's real quota; picking ok/low/out pins it (a pause escape)."""
        combo = QComboBox()
        combo.setStyleSheet("font-size: 10px;")
        combo.addItem("Auto", "auto")
        for tid in _TOKEN_ORDER:  # ok / low / out
            idx = combo.count()
            combo.addItem(f"{_TOKEN_GLYPH[tid]} {tid}", tid)
            combo.setItemData(idx, QColor(_TOKEN_COLOR[tid]), Qt.ItemDataRole.ForegroundRole)
        combo.setCurrentIndex(0 if auto else max(0, combo.findData(tokens)))
        combo.setToolTip(
            "Token-budget setting. Auto derives ok/low/out from the node's real "
            "remaining quota (see the quota row); picking a value pins the state "
            "until set back to Auto. The mesh skips 'out' nodes."
        )
        combo.activated.connect(
            lambda _i, c=combo: self.store.mesh_set_attr(node_id, {"tokens": c.currentData()})
        )
        return combo

    def _quota_row(self, node: dict) -> QHBoxLayout:
        """Read-only quota indicator (separate from the token-budget input): the
        effective state's glyph + color, and the real remaining percentages per
        rate-limit window (5-hour session · 7-day week) when the node's probe has
        them — else the local '≈NN%' estimate. 'pinned' flags a manual override."""
        tokens = node.get("tokens", "ok")
        sess, week = node.get("tokensSessionPct"), node.get("tokensWeekPct")
        if isinstance(sess, (int, float)):
            left = f"5h {round(sess * 100)}%"
            if isinstance(week, (int, float)):
                left += f" · wk {round(week * 100)}%"
            left += " left"
        elif isinstance(node.get("tokensPct"), (int, float)):
            left = f"≈{round(node['tokensPct'] * 100)}% left"
        else:
            left = tokens
        if not node.get("tokensAuto", True):
            left += " · pinned"
        row = QHBoxLayout()
        row.setSpacing(4)
        cap = QLabel("quota")
        cap.setStyleSheet(muted(9))
        row.addWidget(cap)
        color = _TOKEN_COLOR.get(tokens, "#8E8E93")
        val = QLabel(f"{_TOKEN_GLYPH.get(tokens, '●')} {left}")
        val.setStyleSheet(f"color: {color}; font-size: 9px; font-weight: 700;")
        val.setToolTip(
            "Remaining Claude quota — the account's real rate-limit windows "
            "(5-hour session · 7-day week) via the OAuth usage probe; '≈' marks a "
            "local estimate (probe unavailable). 'pinned' = a manual override is in "
            "effect and the mesh routes on it."
        )
        row.addWidget(val)
        row.addStretch(1)
        return row

    def _ban_reason(self, peer: dict) -> str:
        """The recorded reason this peer's device was banned (tooltip text)."""
        entries = (self.store.mesh_state or {}).get("banned") or []
        fp, nid = peer.get("fingerprint", ""), peer.get("id", "")
        for e in entries:
            if e.get("fingerprint"):
                if e["fingerprint"] == fp:
                    return e.get("reason") or "banned"
            elif e.get("node") == nid:
                return e.get("reason") or "banned"
        return "banned"

    def _trust_toggle(self, peer: dict) -> QHBoxLayout:
        """A Personal | Foreign segmented toggle for a peer's device. 'Personal' adds
        its proven key fingerprint to the local allowlist (its mesh requests then run
        here as if triggered locally); 'Foreign' removes it. Disabled until the peer
        proves a device key, since trust must key on a verified fingerprint."""
        row = QHBoxLayout()
        row.setSpacing(4)
        lbl = QLabel("trust")
        lbl.setStyleSheet(muted(9))
        row.addWidget(lbl)

        current = peer.get("trust", "personal")
        fp = peer.get("fingerprint", "")
        verified = bool(peer.get("verified"))
        name = peer.get("name", "")

        if current == "banned":
            # A banned device gets a mark + an Unban escape hatch instead of the
            # toggle: it broke the foreign-accountability contract (accepted a
            # SzpontRequest, never delivered), and stays declined until the
            # operator explicitly lifts the ban.
            meta = _TRUST_META.get("banned", {})
            color = meta.get("colorHex", "#FF3B30")
            mark = QLabel(f"{meta.get('linuxGlyph', '⊘')} banned")
            mark.setStyleSheet(f"color: {color}; font-size: 9px; font-weight: 700;")
            mark.setToolTip(self._ban_reason(peer))
            row.addWidget(mark)
            unban = QToolButton()
            unban.setText("unban")
            unban.setCursor(Qt.CursorShape.PointingHandCursor)
            unban.setStyleSheet(
                "QToolButton { border: none; font-size: 9px; padding: 1px 6px;"
                " border-radius: 6px; color: palette(mid); }")
            node_id = peer.get("id", "")
            unban.clicked.connect(
                lambda: self.store.mesh_unban(fp if verified else "", node_id))
            row.addWidget(unban)
            row.addStretch(1)
            return row

        def seg(level: str) -> QToolButton:
            meta = _TRUST_META.get(level, {})
            b = QToolButton()
            b.setText(f"{meta.get('linuxGlyph', '')} {level}".strip())
            b.setCheckable(True)
            b.setChecked(current == level)
            b.setEnabled(bool(fp))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            active = current == level
            color = meta.get("colorHex", "#8E8E93")
            b.setStyleSheet(
                "QToolButton { border: none; font-size: 9px; font-weight: 700;"
                f" padding: 1px 6px; border-radius: 6px; color: {color if active else 'palette(mid)'};"
                f" background-color: {tint_bg(color, 0.18) if active else 'transparent'}; }}"
                "QToolButton:disabled { color: rgba(128,128,128,0.35); }"
            )
            return b

        personal = seg("personal")
        personal.clicked.connect(lambda: self.store.mesh_trust(fp, name))
        foreign = seg("foreign")
        foreign.clicked.connect(lambda: self.store.mesh_untrust(fp))
        row.addWidget(personal)
        row.addWidget(foreign)
        row.addStretch(1)

        if not fp:
            personal.setToolTip("This device hasn't proven a key yet.")
        elif not verified:
            note = QLabel("(unverified key)")
            note.setStyleSheet(muted(8))
            note.setToolTip("The peer advertises this key but hasn't yet signed our "
                            "challenge — trust applies once it does.")
            row.addWidget(note)
        return row

    # MARK: duties

    def _rebuild_duties(self, state: dict, self_node: dict,
                        peers: list[dict]) -> None:
        _clear_layout(self.duties_col)
        head = QLabel("DUTIES")
        head.setStyleSheet(
            muted(9, bold=True)
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

        # Title row: monochrome duty glyph (same as its grid action card) + title
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        d_glyph = duty.get("linuxGlyph", duty["emoji"])
        title_row.addWidget(GlyphLabel(d_glyph, 16, duty["colorHex"], font_px=13),
                            0, Qt.AlignmentFlag.AlignVCenter)
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
            nobody.setStyleSheet(muted(10))
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
                pglyph, _ = _platform_meta(plat)
                parts.append(f"{cnt}×{pglyph}")
            spread = QLabel("spread: " + "+".join(parts))
            spread.setStyleSheet(
                muted(9, mono=True)
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
