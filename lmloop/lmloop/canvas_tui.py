"""Terminal knowledge canvas: projection helpers and key bindings.

Pure functions are tested without a TTY. The full-screen app is optional
``prompt_toolkit`` and never imports agent, loop, or graph.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Viewport:
    origin_x: float = 0.0
    origin_y: float = 0.0
    width: int = 40
    height: int = 16
    zoom: float = 1.0


def cell_of(node, viewport: Viewport) -> tuple[int, int]:
    """Project a node with ``x``/``y`` into a character cell."""
    zoom = viewport.zoom or 1.0
    x = int((float(node.x) - viewport.origin_x) * zoom)
    y = int((float(node.y) - viewport.origin_y) * zoom)
    return x, y


def cull_viewport(nodes, viewport: Viewport) -> list:
    """Drop nodes whose cell sits outside the pane."""
    kept = []
    for node in nodes:
        x, y = cell_of(node, viewport)
        if 0 <= x < viewport.width and 0 <= y < viewport.height:
            kept.append(node)
    return kept


def bucket_collisions(nodes, viewport: Viewport) -> dict[tuple[int, int], list]:
    """Group on-screen nodes that share a cell. Off-screen nodes are absent."""
    buckets: dict[tuple[int, int], list] = {}
    for node in cull_viewport(nodes, viewport):
        buckets.setdefault(cell_of(node, viewport), []).append(node)
    return buckets


def collision_glyph(members: list) -> str:
    count = len(members)
    if count <= 1:
        return "▣"
    if count < 10:
        return f"▣{count}"
    return "▣+"


def neighbors_of(node_id: str, edges) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for edge in edges:
        src, dst, kind = edge[0], edge[1], edge[2] if len(edge) > 2 else ""
        if src == node_id:
            found.append((kind or "edge", dst))
        elif dst == node_id:
            found.append((kind or "edge", src))
    return found


@dataclass
class CanvasSession:
    """Keyboard state. Bindings call these helpers; no terminal required."""

    nodes: tuple = ()
    edges: tuple = ()
    viewport: Viewport = field(default_factory=Viewport)
    selected: int = 0
    type_filter: "frozenset[str]" = field(default_factory=frozenset)
    query: str = ""
    show_clusters: bool = False
    quit: bool = False
    search_mode: bool = False
    reload_requested: bool = False
    _type_cycle: int = 0

    def filtered_nodes(self) -> list:
        nodes = list(self.nodes)
        if self.type_filter:
            nodes = [node for node in nodes if getattr(node, "type", "") in self.type_filter]
        if self.query:
            needle = self.query.lower()
            nodes = [
                node for node in nodes
                if needle in (getattr(node, "label", "") or "").lower()
                or needle in (getattr(node, "id", "") or "").lower()
                or needle in (getattr(node, "detail", "") or "").lower()
            ]
        return nodes

    def visible_nodes(self) -> list:
        return cull_viewport(self.filtered_nodes(), self.viewport)

    def selected_node(self):
        visible = self.visible_nodes()
        if not visible:
            return None
        index = self.selected % len(visible)
        return visible[index]

    def handle(self, key: str) -> None:
        token = (key or "").lower()
        if self.search_mode:
            self._handle_search(key)
            return
        pan = {
            "h": (-1.0, 0.0), "l": (1.0, 0.0), "j": (0.0, 1.0), "k": (0.0, -1.0),
            "left": (-1.0, 0.0), "right": (1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0),
        }
        if token in pan:
            dx, dy = pan[token]
            zoom = self.viewport.zoom or 1.0
            self.viewport = Viewport(
                origin_x=self.viewport.origin_x + dx / zoom,
                origin_y=self.viewport.origin_y + dy / zoom,
                width=self.viewport.width,
                height=self.viewport.height,
                zoom=zoom,
            )
            return
        if token in ("+", "="):
            self.viewport = _zoom(self.viewport, min(8.0, self.viewport.zoom * 1.25))
            return
        if token == "-":
            self.viewport = _zoom(self.viewport, max(0.25, self.viewport.zoom / 1.25))
            return
        if token == "tab":
            visible = self.visible_nodes()
            if visible:
                self.selected = (self.selected + 1) % len(visible)
            return
        if token in ("enter", "\r"):
            return
        if token == "/":
            self.search_mode = True
            return
        if token == "t":
            self._cycle_type()
            return
        if token == "g":
            self.show_clusters = not self.show_clusters
            return
        if token == "r":
            self.reload_requested = True
            return
        if token == "q":
            self.quit = True

    def _handle_search(self, key: str) -> None:
        if key in ("enter", "\r", "escape"):
            self.search_mode = False
            return
        if key in ("backspace", "\x7f"):
            self.query = self.query[:-1]
            return
        if len(key) == 1 and key.isprintable() and key != "/":
            self.query += key

    def _cycle_type(self) -> None:
        types = []
        seen = set()
        for node in self.nodes:
            typ = getattr(node, "type", "") or ""
            if typ and typ not in seen:
                seen.add(typ)
                types.append(typ)
        if not types:
            self.type_filter = frozenset()
            return
        self._type_cycle = (self._type_cycle + 1) % (len(types) + 1)
        if self._type_cycle == 0:
            self.type_filter = frozenset()
        else:
            self.type_filter = frozenset({types[self._type_cycle - 1]})

    def render(self) -> str:
        width = max(8, self.viewport.width)
        height = max(4, self.viewport.height)
        rows = [[" "] * width for _ in range(height)]
        buckets = bucket_collisions(self.filtered_nodes(), self.viewport)
        for (x, y), members in buckets.items():
            if 0 <= x < width and 0 <= y < height:
                glyph = collision_glyph(members)
                rows[y][x] = glyph[0]
        grid = "\n".join("".join(row).rstrip() for row in rows)
        selected = self.selected_node()
        detail = ["detail: (none)"]
        if selected is not None:
            detail = [
                f"{getattr(selected, 'type', '')}:{getattr(selected, 'id', '')}",
                (getattr(selected, "label", "") or "")[:80],
                f"confidence {getattr(selected, 'confidence', 0)}",
                "neighbors:",
            ]
            hops = neighbors_of(getattr(selected, "id", ""), self.edges)
            if not hops:
                detail.append("  (none)")
            for kind, other in hops:
                detail.append(f"  {kind} {other}")
            cell = cell_of(selected, self.viewport)
            members = buckets.get(cell) or []
            if len(members) > 1:
                detail.append("collision:")
                for member in members:
                    detail.append(f"  {getattr(member, 'id', '')}")
        if self.show_clusters:
            detail.append("clusters:")
            for src, dst, kind in self.edges:
                if kind == "contradicts":
                    detail.append(f"  {src} ≠ {dst}")
        footer = (
            f"/ search={self.query!r}  t types={','.join(sorted(self.type_filter)) or 'all'}"
            f"  g clusters={'on' if self.show_clusters else 'off'}"
            f"  zoom={self.viewport.zoom:.2f}"
        )
        return grid + "\n" + "\n".join(detail) + "\n" + footer


def _zoom(viewport: Viewport, zoom: float) -> Viewport:
    return Viewport(
        origin_x=viewport.origin_x,
        origin_y=viewport.origin_y,
        width=viewport.width,
        height=viewport.height,
        zoom=zoom,
    )


def reload_session(session: CanvasSession, nodes, edges) -> None:
    session.nodes = tuple(nodes)
    session.edges = tuple(edges)
    session.reload_requested = False


def try_fullscreen(load_view) -> bool:
    """Run the full-screen canvas when a TTY and prompt_toolkit exist.

    Returns False so the caller can print the text canvas instead.
    ``load_view(query)`` must return an object with ``nodes`` and ``edges``.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError:
        return False

    view = load_view("")
    session = CanvasSession(
        nodes=tuple(getattr(view, "nodes", ()) or ()),
        edges=tuple(getattr(view, "edges", ()) or ()),
    )
    control = FormattedTextControl(text=lambda: session.render())

    bindings = KeyBindings()

    def _bind(name: str):
        def _handler(_event):
            session.handle(name)
            if session.reload_requested:
                fresh = load_view(session.query)
                reload_session(
                    session,
                    getattr(fresh, "nodes", ()) or (),
                    getattr(fresh, "edges", ()) or (),
                )
            if session.quit:
                _event.app.exit()
        return _handler

    for name in (
        "h", "j", "k", "l", "q", "t", "g", "r", "+", "-", "/",
        "up", "down", "left", "right", "tab", "enter", "escape", "backspace",
    ):
        bindings.add(name)(_bind(name))

    @bindings.add("<any>")
    def _any(event):
        data = event.data or ""
        if session.search_mode and data:
            session.handle(data)

    app = Application(
        layout=Layout(Window(content=control)),
        key_bindings=bindings,
        full_screen=True,
    )
    app.run()
    return True


def open_memory_canvas(cfg: dict, query: str = "") -> str:
    """Full-screen when possible; otherwise the text canvas (or graph-off message)."""
    from .config import cfg_bool
    from . import knowledge_graph

    if not cfg_bool(cfg, "use_graph"):
        return knowledge_graph.MSG_GRAPH_OFF
    kg = knowledge_graph.KnowledgeGraph()
    kg.ensure(cfg)

    def load_view(text: str):
        return kg.canvas_view(query=text or query)

    if try_fullscreen(load_view):
        return ""
    return knowledge_graph.format_canvas_text(cfg, query=query)
