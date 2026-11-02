#!/usr/bin/env python3
"""
xfce-overview — a GNOME-style "Activities Overview" for XFCE (xfwm4 + X11).

  * one overview window PER MONITOR; each shows only the windows that live on it
  * shows only the windows of the CURRENT workspace, with a workspace strip to
    switch (set WORKSPACE_SCOPE = "all" for one card per workspace instead)
  * click a window to switch, middle-click closes, right-click = menu
  * DRAG a window onto a workspace pill (or workspace card in "all" scope) to move
    it to that workspace
  * type-to-search installed applications (.desktop files via Gio), Enter launches;
    the search bar is shown ONLY on the primary monitor (see SEARCH_ON)
  * single-instance daemon (Gtk.Application / D-Bus); every activation toggles

Usage:
    xfce-overview --daemon     start hidden in the background (autostart this)
    xfce-overview              toggle (starts the daemon first if needed)
    xfce-overview --quit       stop the daemon
"""
import json
import math
import os
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Wnck", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gdk, GdkX11, GdkPixbuf, Gio, GLib, Pango, Wnck  # noqa: E402

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
APP_ID = "io.github.local.XfceOverview"
PER_MONITOR = True               # True: one overview per monitor, windows split by monitor
                                 # False: single overview on the monitor under the pointer
WORKSPACE_SCOPE = "current"      # "current": only the active workspace's windows + switcher strip
                                 # "all": one card per workspace (previous behaviour)
USE_FULLSCREEN = True            # False: plain undecorated windows placed by coordinates
                                 # (use if fullscreen misbehaves; xfce4-panel may stay visible)
CACHE_W, CACHE_H = 480, 300      # max size of stored snapshots (~560 KB each)
LIVE_PREVIEWS = True             # refresh thumbnails while open (needs compositor)
LIVE_REFRESH_MS = 500            # one window is re-captured per tick (round-robin)
MAX_RESULTS = 10                 # app search results shown
SEARCH_ON = "primary"            # "primary": search bar only on the primary monitor (xfce display settings)
                                 # "pointer": search bar only on the monitor under the mouse

CSS = b"""
#overview { background-color: rgba(16, 16, 20, 0.88); }
#overview.opaque { background-color: #14141a; }
#overview scrolledwindow, #overview viewport { background-color: transparent; }

#search {
    min-height: 42px; font-size: 15pt; padding: 0 14px; border-radius: 22px;
    color: #ffffff; background-color: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.18); box-shadow: none;
}
#search:focus { border-color: #3584e4; }

.ws-pill { background-color: rgba(255,255,255,0.08); border: 2px solid transparent;
           border-radius: 16px; padding: 6px 18px; }
.ws-pill:hover { background-color: rgba(255,255,255,0.16); }
.ws-pill.current { background-color: rgba(53,132,228,0.30); border-color: #3584e4; }
.pill-label { color: #ffffff; font-size: 10pt; }

.ws-card { background-color: rgba(255,255,255,0.04);
           border: 2px solid transparent; border-radius: 16px; }
.ws-card.current { background-color: rgba(255,255,255,0.07);
                   border-color: rgba(53,132,228,0.55); }
.ws-title { color: rgba(255,255,255,0.85); font-weight: bold; font-size: 12pt; }
.dim { color: rgba(255,255,255,0.45); }

.tile { background-color: rgba(255,255,255,0.06);
        border: 2px solid transparent; border-radius: 12px; }
/* hover / keyboard-selected highlight: change the colours here */
.tile:hover, .tile.hover, .tile.selected {
    background-color: rgba(255,255,255,0.16);
    border-color: #4a9dff;
    box-shadow: 0 0 16px rgba(74,157,255,0.60);
}
.tile:hover .thumb, .tile.hover .thumb, .tile.selected .thumb {
    background-color: rgba(0,0,0,0.15);
}
.tile:hover .tile-title, .tile.hover .tile-title, .tile.selected .tile-title {
    color: #ffffff;
}
.tile-title { color: #f0f0f0; font-size: 10pt; }
.thumb { background-color: rgba(0,0,0,0.35); border-radius: 8px; }

/* drag & drop: drop-target highlight and the tile being dragged */
.ws-pill.drop-hover { background-color: rgba(53,132,228,0.45); border-color: #4a9dff;
                      box-shadow: 0 0 14px rgba(74,157,255,0.60); }
.ws-card.drop-hover { background-color: rgba(53,132,228,0.18); border-color: #4a9dff; }

#results { background-color: transparent; }
#results row { background-color: rgba(255,255,255,0.05); border-radius: 12px;
               margin: 3px 0; padding: 4px; }
#results row:hover { background-color: rgba(255,255,255,0.12); }
#results row:selected { background-color: rgba(53,132,228,0.45); }
.app-name { color: #ffffff; font-size: 12pt; font-weight: bold; }
.app-desc { color: rgba(255,255,255,0.6); font-size: 9pt; }
"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def trap_push():
    try:
        Gdk.error_trap_push()
    except Exception:
        pass


def trap_pop():
    """Pop the X error trap; returns non-zero if an X error occurred."""
    try:
        Gdk.flush()
        return Gdk.error_trap_pop()
    except Exception:
        return 0


def fit_pixbuf(pb, tw, th):
    """Scale pb down (never up) to fit inside tw x th, keeping aspect ratio."""
    w, h = pb.get_width(), pb.get_height()
    s = min(tw / w, th / h, 1.0)
    if s >= 0.999:
        return pb
    return pb.scale_simple(max(1, int(w * s)), max(1, int(h * s)),
                           GdkPixbuf.InterpType.BILINEAR)


def add_class(widget, name):
    widget.get_style_context().add_class(name)


DND_TARGET = "application/x-xfce-overview-window"


def dnd_entry():
    return Gtk.TargetEntry.new(DND_TARGET, Gtk.TargetFlags.SAME_APP, 0)


def event_in_tile(ev):
    """True if a (possibly propagated) event originated inside a window tile."""
    w = Gtk.get_event_widget(ev)
    while w is not None:
        if w.get_style_context().has_class("tile"):
            return True
        w = w.get_parent()
    return False


def clear_children(container):
    for child in container.get_children():
        child.destroy()


def rect_index(rects, x, y):
    """Index of the (x, y, w, h) rect containing the point, else the nearest one."""
    best, best_d = 0, None
    for k, (rx, ry, rw, rh) in enumerate(rects):
        dx = max(rx - x, 0, x - (rx + rw - 1))
        dy = max(ry - y, 0, y - (ry + rh - 1))
        d = dx * dx + dy * dy
        if d == 0:
            return k
        if best_d is None or d < best_d:
            best, best_d = k, d
    return best


# --------------------------------------------------------------------------- #
# Application index (.desktop files)
# --------------------------------------------------------------------------- #
class AppIndex:
    def __init__(self):
        self.apps = []
        self.wm_map, self.id_map = {}, {}
        self.usage_path = os.path.join(GLib.get_user_data_dir(),
                                       "xfce-overview", "usage.json")
        self.usage = self._load_usage()
        self.reload()
        # keep the reference, otherwise the monitor is garbage collected
        self._monitor = Gio.AppInfoMonitor.get()
        self._monitor.connect("changed", lambda *_: self.reload())

    def _load_usage(self):
        try:
            with open(self.usage_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def record(self, app_id):
        self.usage[app_id] = self.usage.get(app_id, 0) + 1
        try:
            os.makedirs(os.path.dirname(self.usage_path), exist_ok=True)
            with open(self.usage_path, "w") as f:
                json.dump(self.usage, f)
        except Exception:
            pass

    def reload(self):
        apps, wm_map, id_map = [], {}, {}
        for info in Gio.AppInfo.get_all():
            if not info.should_show():
                continue
            name = info.get_name() or ""
            generic, keywords, wm = "", [], ""
            if isinstance(info, Gio.DesktopAppInfo):
                generic = info.get_generic_name() or ""
                keywords = info.get_keywords() or []
                wm = info.get_startup_wm_class() or ""
            exe = os.path.basename(info.get_executable() or "")
            app_id = info.get_id() or name
            entry = {
                "info": info,
                "id": app_id,
                "name": name,
                "name_l": name.casefold(),
                "extra_l": " ".join([generic, " ".join(keywords), exe]).casefold(),
                "desc_l": (info.get_description() or "").casefold(),
            }
            apps.append(entry)
            if wm:
                wm_map.setdefault(wm.lower(), entry)
            base = app_id[:-8] if app_id.endswith(".desktop") else app_id
            id_map.setdefault(base.lower(), entry)
            id_map.setdefault(base.split(".")[-1].lower(), entry)   # org.x.Foo -> foo
            if exe:
                id_map.setdefault(exe.lower(), entry)
        self.apps, self.wm_map, self.id_map = apps, wm_map, id_map

    def match_window(self, class_names):
        """Map WM_CLASS strings of a window to a .desktop entry (or None)."""
        for c in class_names:
            if c in self.wm_map:
                return self.wm_map[c]
        for c in class_names:
            if c in self.id_map:
                return self.id_map[c]
        return None

    def search(self, query):
        q = query.casefold().strip()
        if not q:
            return []
        toks = q.split()
        scored = []
        for a in self.apps:
            n = a["name_l"]
            if n == q:
                s = 0
            elif n.startswith(q):
                s = 1
            elif any(w.startswith(q) for w in n.replace("-", " ").split()):
                s = 2
            elif q in n:
                s = 3
            elif q in a["extra_l"]:
                s = 4
            elif all(t in n or t in a["extra_l"] for t in toks):
                s = 5
            elif q in a["desc_l"]:
                s = 6
            else:
                continue
            scored.append((s, -self.usage.get(a["id"], 0), n, a))
        scored.sort(key=lambda r: r[:3])
        return [r[3] for r in scored[:MAX_RESULTS]]


# --------------------------------------------------------------------------- #
# Window snapshots
# --------------------------------------------------------------------------- #
class Snapshots:
    """
    Grabs window contents with gdk_pixbuf_get_from_window() on a foreign GdkWindow.
    With the xfwm4 compositor ON the window contents live in an off-screen pixmap,
    so even partly covered windows capture correctly. With it OFF, only a window
    that is completely unobscured can be captured reliably.
    Unmapped windows (minimized / other workspace on xfwm4) cannot be captured;
    the last good snapshot stays in the cache and is used instead.
    """

    def __init__(self):
        self.cache = {}                       # xid -> GdkPixbuf (<= CACHE_W x CACHE_H)

    def forget(self, xid):
        self.cache.pop(xid, None)

    def capture(self, win):
        try:
            _x, _y, w, h = win.get_client_window_geometry()
            if w < 8 or h < 8:
                return None
            display = Gdk.Display.get_default()
            gw = GdkX11.X11Window.foreign_new_for_display(display, win.get_xid())
            if gw is None:
                return None
            trap_push()
            try:
                pb = Gdk.pixbuf_get_from_window(gw, 0, 0, w, h)
            except Exception:
                pb = None
            if trap_pop() or pb is None:
                return None
            s = min(CACHE_W / w, CACHE_H / h, 1.0)
            if s < 1.0:
                pb = pb.scale_simple(max(1, int(w * s)), max(1, int(h * s)),
                                     GdkPixbuf.InterpType.BILINEAR)
            self.cache[win.get_xid()] = pb
            return pb
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# One overview window (one per monitor)
# --------------------------------------------------------------------------- #
class Panel:
    def __init__(self, app, pos, mon_index, rect):
        self.app = app
        self.pos = pos                  # position in app.panels
        self.mon_index = mon_index      # GDK monitor index (for fullscreen_on_monitor)
        self.rect = rect                # (x, y, w, h) of the monitor
        self.mon_w, self.mon_h = rect[2], rect[3]
        self.is_main = False
        self.tiles = []                 # [(EventBox, wnck window)]
        self._build()

    def _build(self):
        app = self.app
        w = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win = w
        w.set_name("overview")
        w.set_title("Overview")
        w.set_decorated(False)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_keep_above(True)
        w.set_app_paintable(True)
        w.stick()                       # visible on every workspace (survives switching)
        vis = w.get_screen().get_rgba_visual()
        if vis is not None:
            w.set_visual(vis)
        w.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        w.connect("key-press-event", lambda _w, ev: app.on_key(self, ev))
        w.connect("button-press-event", lambda _w, ev: app.on_bg_click(ev))
        w.connect("delete-event", lambda *_: (app.hide_overview(), True)[1])

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, "set_margin_" + side)(40)

        self.entry = Gtk.SearchEntry()
        self.entry.set_name("search")
        self.entry.set_size_request(560, -1)
        self.entry.set_halign(Gtk.Align.CENTER)
        self.entry.set_placeholder_text("Type to search applications…")
        self.entry.connect("search-changed", lambda e: app.on_search_changed(self, e))
        self.entry.connect("activate", lambda e: app.on_entry_activate(self))
        root.pack_start(self.entry, False, False, 0)

        self.strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.strip.set_halign(Gtk.Align.CENTER)
        root.pack_start(self.strip, False, False, 0)

        self.stack = Gtk.Stack()

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_shadow_type(Gtk.ShadowType.NONE)
        self.holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        self.holder.set_halign(Gtk.Align.CENTER)
        self.holder.set_valign(Gtk.Align.START)
        sw.add(self.holder)
        self.stack.add_named(sw, "windows")

        sw2 = Gtk.ScrolledWindow()
        sw2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw2.set_shadow_type(Gtk.ShadowType.NONE)
        self.listbox = Gtk.ListBox()
        self.listbox.set_name("results")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_size_request(560, -1)
        self.listbox.set_halign(Gtk.Align.CENTER)
        self.listbox.set_valign(Gtk.Align.START)
        self.listbox.connect("row-activated", lambda _lb, row: app.on_row_activated(row))
        sw2.add(self.listbox)
        self.stack.add_named(sw2, "apps")

        root.pack_start(self.stack, True, True, 0)
        w.add(root)
        root.show_all()          # children visible; the toplevel stays hidden
        w.realize()              # we need a GdkWindow for X server timestamps

    def set_main(self, flag):
        """Only the main panel (primary monitor) shows the search box; it is hidden
        completely elsewhere so it does not reserve any space."""
        self.is_main = flag
        self.entry.set_sensitive(flag)
        self.entry.set_visible(flag)

    def clear(self):
        clear_children(self.holder)
        clear_children(self.strip)
        clear_children(self.listbox)
        self.tiles = []
        self.entry.set_text("")
        self.stack.set_visible_child_name("windows")

    # ---- windows view -------------------------------------------------------
    def rebuild(self, shown_ws, by_ws, active_n, all_ws):
        clear_children(self.holder)
        clear_children(self.strip)
        self.tiles = []

        scope_current = WORKSPACE_SCOPE == "current"
        if scope_current and len(all_ws) > 1:
            for ws in all_ws:
                self.strip.pack_start(self._make_pill(ws, ws.get_number() == active_n),
                                      False, False, 0)

        n_shown = max(1, len(shown_ws))
        card_w = max(320, (self.mon_w - 120 - 24 * (n_shown - 1)) // n_shown)
        show_title = (not scope_current) and len(all_ws) > 1
        for ws in shown_ws:
            card = self._make_card(ws, by_ws.get(ws.get_number(), []), n_shown,
                                   show_title, card_w, ws.get_number() == active_n,
                                   not scope_current)
            self.holder.pack_start(card, False, False, 0)
        self.holder.show_all()
        self.strip.show_all()

    def _make_pill(self, ws, is_current):
        pill = Gtk.EventBox()
        add_class(pill, "ws-pill")
        if is_current:
            add_class(pill, "current")
        lbl = Gtk.Label(label=ws.get_name() or str(ws.get_number() + 1))
        add_class(lbl, "pill-label")
        pill.add(lbl)
        pill.connect("button-press-event",
                     lambda _p, ev, ws=ws: self.app.on_pill_click(ev, ws))
        self._make_drop_target(pill, ws)
        return pill

    def _tile_geometry(self, n, card_w):
        usable = card_w - 24

        def tw_for(c):
            return min(460, (usable - 12 * (c - 1)) // c - 24)

        cols = 1 if n <= 1 else math.ceil(math.sqrt(n))
        while cols > 1 and tw_for(cols) < 170:
            cols -= 1
        tw = max(120, tw_for(cols))
        return cols, tw, int(tw * CACHE_H / CACHE_W)

    def _make_card(self, ws, wins, n_shown, show_title, card_w, is_current, clickable):
        card = Gtk.EventBox()
        add_class(card, "ws-card")
        if is_current:
            add_class(card, "current")
        card.set_valign(Gtk.Align.START)
        if n_shown > 1:
            card.set_size_request(card_w, -1)
        if clickable:                       # "all" scope: click card = go to that workspace
            card.connect("button-press-event",
                         lambda _c, ev, ws=ws: self.app.on_ws_click(ev, ws))
            self._make_drop_target(card, ws)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(12)
        card.add(box)

        if show_title:
            title = Gtk.Label(label="%d  ·  %s" % (ws.get_number() + 1, ws.get_name()))
            title.set_xalign(0)
            add_class(title, "ws-title")
            box.pack_start(title, False, False, 0)

        if not wins:
            lbl = Gtk.Label(label="No windows")
            add_class(lbl, "dim")
            lbl.set_size_request(-1, 140)
            box.pack_start(lbl, True, True, 0)
            return card

        cols, tw, th = self._tile_geometry(len(wins), card_w)
        grid = Gtk.Grid()
        grid.set_row_spacing(12)
        grid.set_column_spacing(12)
        grid.set_halign(Gtk.Align.CENTER)
        for i, w in enumerate(wins):
            grid.attach(self._make_tile(w, tw, th), i % cols, i // cols, 1, 1)
        box.pack_start(grid, False, False, 0)
        return card

    # ---- drag & drop --------------------------------------------------------
    def _make_drag_source(self, tile, w):
        tile.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [dnd_entry()],
                             Gdk.DragAction.MOVE)
        tile.connect("drag-begin", lambda t, ctx, w=w: self.app.on_drag_begin(t, ctx, w))
        tile.connect("drag-data-get",
                     lambda _t, _c, data, _i, _tm, w=w:
                     data.set(data.get_target(), 8, str(w.get_xid()).encode()))
        tile.connect("drag-end", lambda t, _c: self.app.on_drag_end(t))

    def _make_drop_target(self, widget, ws):
        widget.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
                             [dnd_entry()], Gdk.DragAction.MOVE)
        widget.connect("drag-motion",
                       lambda wd, _c, _x, _y, _t, ws=ws: self._on_drop_motion(wd, ws))
        widget.connect("drag-leave", lambda wd, _c, _t: self._on_drop_leave(wd))
        widget.connect("drag-data-received",
                       lambda _w, _c, _x, _y, data, _i, _t, ws=ws:
                       self.app.on_window_dropped(data, ws))

    def _on_drop_motion(self, widget, ws):
        if self.app.can_drop_on(ws):
            add_class(widget, "drop-hover")
        return False                      # let GTK report the drop action

    def _on_drop_leave(self, widget):
        widget.get_style_context().remove_class("drop-hover")

    def _on_tile_enter(self, tile, _ev):
        add_class(tile, "hover")
        return False

    def _on_tile_leave(self, tile, ev):
        if ev.detail != Gdk.NotifyType.INFERIOR:      # ignore moves onto our own children
            tile.get_style_context().remove_class("hover")
        return False

    def _set_hand_cursor(self, tile):
        try:
            gw = tile.get_window()
            cur = Gdk.Cursor.new_from_name(tile.get_display(), "pointer")
            if gw is not None and cur is not None:
                gw.set_cursor(cur)
        except Exception:
            pass

    def _make_tile(self, w, tw, th):
        app = self.app
        tile = Gtk.EventBox()
        add_class(tile, "tile")
        self._make_drag_source(tile, w)
        # left click acts on RELEASE (a press may be the start of a drag)
        tile.connect("button-press-event", lambda _t, ev, w=w: app.on_tile_press(ev, w))
        tile.connect("button-release-event",
                     lambda t, ev, w=w: app.on_tile_release(t, ev, w))
        # explicit hover handling (does not depend on theme/pseudo-class behaviour)
        tile.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        tile.connect("enter-notify-event", self._on_tile_enter)
        tile.connect("leave-notify-event", self._on_tile_leave)
        tile.connect("realize", self._set_hand_cursor)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(8)
        tile.add(box)

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = app.icon_for(w, 20)
        if icon is not None:
            hdr.pack_start(Gtk.Image.new_from_pixbuf(icon), False, False, 0)
        title = Gtk.Label(label=w.get_name() or "")
        title.set_xalign(0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(max(8, tw // 8))
        add_class(title, "tile-title")
        hdr.pack_start(title, True, True, 0)
        box.pack_start(hdr, False, False, 0)

        thumb = Gtk.Box()
        add_class(thumb, "thumb")
        thumb.set_size_request(tw, th)
        img = Gtk.Image()
        img.set_halign(Gtk.Align.CENTER)
        img.set_valign(Gtk.Align.CENTER)
        thumb.pack_start(img, True, True, 0)
        box.pack_start(thumb, False, False, 0)

        xid = w.get_xid()
        snap = app.snaps.cache.get(xid)
        if snap is not None:
            img.set_from_pixbuf(fit_pixbuf(snap, tw, th))
        else:                                    # fallback: big static app icon
            big = app.icon_for(w, 64)
            if big is not None:
                img.set_from_pixbuf(big)
        app.live[xid] = (w, img, tw, th)
        self.tiles.append((tile, w))
        return tile


# --------------------------------------------------------------------------- #
# The application / controller
# --------------------------------------------------------------------------- #
class Overview(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    # ---- Gtk.Application plumbing ------------------------------------------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()                                   # stay alive while hidden
        Wnck.set_client_type(Wnck.ClientType.PAGER)   # WM treats us like a pager
        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.apps = AppIndex()
        self.snaps = Snapshots()
        self.icon_cache = {}
        self.panels = []
        self.main = None
        self._panel_key = None
        self.is_open = False
        self.tiles = []          # all tiles, main panel first (keyboard navigation)
        self.sel = -1
        self.live = {}           # xid -> (wnck win, Gtk.Image, tw, th)
        self.results = []
        self._rb_src = 0
        self._live_src = 0
        self._live_i = 0
        self._menu = None
        self.dragging = False    # a tile is being dragged right now
        self.drag_win = None     # ... and this is its wnck window
        self._rb_pending = False # a rebuild was postponed until the drag ends

        self.screen.connect("active-window-changed", self._on_active_changed)
        self.screen.connect("window-closed", self._on_window_closed)
        for sig in ("window-opened", "workspace-created", "workspace-destroyed",
                    "active-workspace-changed"):
            self.screen.connect(sig, lambda *_: self._rebuild_soon(250))

        for s in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, s, self._on_signal)

    def _on_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):                 # D-Bus "Activate" (gdbus call ...)
        self.toggle()

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--quit" in args:
            self.quit()
        elif "--daemon" not in args:
            self.toggle()
        return 0

    # ---- monitors & panels --------------------------------------------------
    def _monitors(self):
        """-> ([(gdk_index, (x, y, w, h)), ...], pointer monitor idx, primary monitor idx)"""
        display = Gdk.Display.get_default()
        mons, primary = [], None
        for i in range(display.get_n_monitors()):
            mon = display.get_monitor(i)
            g = mon.get_geometry()
            mons.append((i, (g.x, g.y, g.width, g.height)))
            if primary is None and mon.is_primary():
                primary = i
        px = py = 0
        try:
            _scr, px, py = display.get_default_seat().get_pointer().get_position()
        except Exception:
            pass
        ptr = rect_index([m[1] for m in mons], px, py)
        return mons, ptr, (primary if primary is not None else ptr)

    def _ensure_panels(self):
        mons, ptr, primary = self._monitors()
        specs = mons if PER_MONITOR else [mons[ptr]]
        if PER_MONITOR:
            main_pos = primary if SEARCH_ON == "primary" else ptr
        else:
            main_pos = 0
        key = tuple(specs)
        if key != self._panel_key:
            for p in self.panels:
                p.win.destroy()
            self.panels = [Panel(self, pos, idx, rect)
                           for pos, (idx, rect) in enumerate(specs)]
            self._panel_key = key
        self.main = self.panels[main_pos]
        for p in self.panels:
            p.set_main(p is self.main)

    def _panel_for(self, w):
        """Which panel (monitor) a window belongs to: the one under its centre point."""
        if not PER_MONITOR or len(self.panels) == 1:
            return 0
        try:
            x, y, ww, hh = w.get_geometry()
            return rect_index([p.rect for p in self.panels], x + ww // 2, y + hh // 2)
        except Exception:
            return 0

    # ---- helpers around the window manager ---------------------------------
    def composited(self):
        return Gdk.Screen.get_default().is_composited()

    def _server_time(self):
        try:
            return GdkX11.x11_get_server_time(self.panels[0].win.get_window())
        except Exception:
            return Gtk.get_current_event_time()

    def _own_xids(self):
        out = set()
        for p in self.panels:
            try:
                out.add(p.win.get_window().get_xid())
            except Exception:
                pass
        return out

    def _is_candidate(self, w, own=None):
        try:
            own = self._own_xids() if own is None else own
            if w.get_xid() in own or w.is_skip_tasklist():
                return False
            return w.get_window_type() in (Wnck.WindowType.NORMAL,
                                           Wnck.WindowType.DIALOG,
                                           Wnck.WindowType.UTILITY)
        except Exception:
            return False

    def _can_capture(self, w):
        """Is the window currently mapped (and, without compositor, safe to grab)?"""
        try:
            if w.is_minimized() or w.is_shaded():
                return False
            ws = self.screen.get_active_workspace()
            if ws is not None and not w.is_on_workspace(ws):
                return False
            if not self.composited():
                active = self.screen.get_active_window()
                return active is not None and active.get_xid() == w.get_xid()
            return True
        except Exception:
            return False

    def _wm_classes(self, w):
        out = []
        try:
            cg = w.get_class_group()
            if cg is not None:
                out.append((cg.get_res_class() or "").lower())
        except Exception:
            pass
        try:
            out.append((w.get_class_instance_name() or "").lower())
        except Exception:
            pass
        return [c for c in out if c]

    def icon_for(self, w, size):
        info = self.apps.match_window(self._wm_classes(w))
        if info is not None:
            key = (info["id"], size)
            if key in self.icon_cache:
                return self.icon_cache[key]
            gicon = info["info"].get_icon()
            if gicon is not None:
                ii = Gtk.IconTheme.get_default().lookup_by_gicon(
                    gicon, size, Gtk.IconLookupFlags.FORCE_SIZE)
                if ii is not None:
                    try:
                        pb = ii.load_icon()
                        self.icon_cache[key] = pb
                        return pb
                    except GLib.Error:
                        pass
        pb = w.get_icon()
        if pb is not None:
            return pb.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        return None

    # ---- Wnck callbacks (daemon is idle between these) ---------------------
    def _on_active_changed(self, _screen, previous):
        """Window just lost focus: it is still mapped, so grab a snapshot now."""
        if self.is_open or previous is None:
            return
        try:
            if (self.composited() and self._is_candidate(previous, set())
                    and self._can_capture(previous)):
                self.snaps.capture(previous)
        except Exception:
            pass

    def _on_window_closed(self, _screen, w):
        try:
            self.snaps.forget(w.get_xid())
        except Exception:
            pass
        self._rebuild_soon(200)

    # ---- show / hide --------------------------------------------------------
    def toggle(self):
        if self.is_open:
            self.hide_overview()
        else:
            self.show_overview()

    def show_overview(self):
        self.screen.force_update()
        self._ensure_panels()
        own = self._own_xids()

        # 1) Snapshot BEFORE we map our windows: without a compositor the overview
        #    would otherwise end up inside the screenshots.
        for w in self.screen.get_windows_stacked():
            if self._is_candidate(w, own) and self._can_capture(w):
                self.snaps.capture(w)

        # 2) Prepare UI
        comp = self.composited()
        for p in self.panels:
            ctx = p.win.get_style_context()
            if comp:
                ctx.remove_class("opaque")
            else:
                ctx.add_class("opaque")
            p.clear()
        self.is_open = True
        self.sel = -1
        self._rebuild()

        # 3) Map them. A fresh server timestamp lets xfwm4's focus-stealing
        #    prevention give us keyboard focus. The main panel goes last so it
        #    ends up focused.
        ts = self._server_time()
        for p in [q for q in self.panels if q is not self.main] + [self.main]:
            self._place_panel(p)
            p.win.present_with_time(ts)
        self.main.entry.grab_focus()
        GLib.timeout_add(400, self._verify_placement)

        if LIVE_PREVIEWS and comp and not self._live_src:
            self._live_src = GLib.timeout_add(LIVE_REFRESH_MS, self._live_tick)

    def _place_panel(self, p):
        """
        Put the panel on ITS monitor by coordinates. xfwm4 fullscreens a window on
        the monitor where the window is positioned, so move first, then fullscreen.
        (fullscreen_on_monitor(index) is NOT used: xfwm4 may number monitors
        differently from GTK, which stacked every overview on the primary screen.)
        """
        x, y, w, h = p.rect
        win = p.win
        win.unfullscreen()
        win.move(x, y)
        win.resize(w, h)
        if USE_FULLSCREEN:
            win.fullscreen()

    def _verify_placement(self):
        """Self-check: if a panel did not land on its monitor, force it there."""
        if not self.is_open:
            return False
        for p in self.panels:
            try:
                gw = p.win.get_window()
                res = gw.get_origin()
                ox, oy = res[-2], res[-1]
                x, y, w, h = p.rect
                if abs(ox - x) > 40 or abs(oy - y) > 40:
                    print("panel %d landed at %d,%d instead of %d,%d - forcing"
                          % (p.pos, ox, oy, x, y), file=sys.stderr)
                    p.win.unfullscreen()
                    p.win.move(x, y)
                    p.win.resize(w, h)
            except Exception as e:
                print("placement check failed:", e, file=sys.stderr)
        return False

    def hide_overview(self):
        if not self.is_open:
            return
        self.is_open = False
        self.dragging, self.drag_win, self._rb_pending = False, None, False
        for attr in ("_rb_src", "_live_src"):
            src = getattr(self, attr)
            if src:
                GLib.source_remove(src)
                setattr(self, attr, 0)
        for p in self.panels:
            p.win.hide()
            p.clear()          # free widgets (and their pixbuf copies) while idle
        self.tiles, self.live, self.results, self.sel = [], {}, [], -1

    # ---- (re)building the windows view -------------------------------------
    def _rebuild(self):
        self.live = {}
        ws_list = self.screen.get_workspaces()
        active = self.screen.get_active_workspace()
        active_n = active.get_number() if active is not None else 0

        if WORKSPACE_SCOPE == "current":
            shown = [ws for ws in ws_list if ws.get_number() == active_n] or ws_list[:1]
        else:
            shown = list(ws_list)
        shown_nums = {ws.get_number() for ws in shown}

        grouped = [{n: [] for n in shown_nums} for _ in self.panels]
        own = self._own_xids()
        for w in reversed(self.screen.get_windows_stacked()):   # top-most first
            if not self._is_candidate(w, own):
                continue
            ws = w.get_workspace()
            num = active_n if (w.is_pinned() or ws is None) else ws.get_number()
            if num not in shown_nums:
                continue
            grouped[self._panel_for(w)][num].append(w)

        for p, g in zip(self.panels, grouped):
            p.rebuild(shown, g, active_n, ws_list)

        order = [self.main] + [p for p in self.panels if p is not self.main]
        self.tiles = [t for p in order for t in p.tiles]
        self._apply_selection()

    def _rebuild_soon(self, ms=150):
        if not self.is_open:
            return
        if self._rb_src:
            GLib.source_remove(self._rb_src)
        self._rb_src = GLib.timeout_add(ms, self._do_rebuild)

    def _do_rebuild(self):
        self._rb_src = 0
        if self.dragging:                 # never destroy a tile mid-drag; redo after drop
            self._rb_pending = True
            return False
        self._rb_pending = False
        if self.is_open and self.main.stack.get_visible_child_name() == "windows":
            if self.composited():       # workspace may have changed: refresh now-visible windows
                own = self._own_xids()
                for w in self.screen.get_windows_stacked():
                    if self._is_candidate(w, own) and self._can_capture(w):
                        self.snaps.capture(w)
            self.sel = -1
            self._rebuild()
        return False

    def _live_tick(self):
        """Near-live previews: re-capture ONE window per tick (cheap, round-robin)."""
        if not self.is_open:
            self._live_src = 0
            return False
        if self.main.stack.get_visible_child_name() != "windows":
            return True
        keys = list(self.live.keys())
        for _ in range(len(keys)):
            xid = keys[self._live_i % len(keys)]
            self._live_i += 1
            w, img, tw, th = self.live[xid]
            if self._can_capture(w):
                pb = self.snaps.capture(w)
                if pb is not None:
                    img.set_from_pixbuf(fit_pixbuf(pb, tw, th))
                break
        return True

    # ---- selection (keyboard navigation of window tiles) -------------------
    def _apply_selection(self):
        for i, (tile, _w) in enumerate(self.tiles):
            ctx = tile.get_style_context()
            if i == self.sel:
                ctx.add_class("selected")
            else:
                ctx.remove_class("selected")

    def _move_sel(self, d):
        n = len(self.tiles)
        if not n:
            return
        if self.sel < 0:
            self.sel = 0 if d > 0 else n - 1
        else:
            self.sel = (self.sel + d) % n
        self._apply_selection()

    def _move_row(self, d):
        lb = self.main.listbox
        rows = [r for r in lb.get_children() if r.get_selectable()]
        if not rows:
            return
        cur = lb.get_selected_row()
        i = rows.index(cur) if cur in rows else -1
        lb.select_row(rows[max(0, min(len(rows) - 1, i + d))])

    # ---- actions ------------------------------------------------------------
    def _activate_window(self, w):
        ts = self._server_time()
        self.hide_overview()
        try:
            ws = w.get_workspace()
            cur = self.screen.get_active_workspace()
            if ws is not None and (cur is None or ws.get_number() != cur.get_number()):
                ws.activate(ts)
            if w.is_minimized():
                w.unminimize(ts)
            w.activate(ts)
        except Exception as e:
            print("activate failed:", e, file=sys.stderr)

    def _close_window(self, w):
        try:
            w.close(self._server_time())
        except Exception:
            pass
        self._rebuild_soon(300)

    def _launch(self, app):
        ts = self._server_time()
        self.hide_overview()                 # release focus first, then launch
        try:
            ctx = Gdk.Display.get_default().get_app_launch_context()
            ctx.set_timestamp(ts)
            app["info"].launch([], ctx)
        except GLib.Error as e:
            print("launch failed:", e.message, file=sys.stderr)
        except Exception as e:
            print("launch failed:", e, file=sys.stderr)
        self.apps.record(app["id"])

    # ---- event handlers (called by the panels) -----------------------------
    def on_bg_click(self, ev):
        if event_in_tile(ev):                # press on a tile may start a drag
            return True
        if ev.button == 1:                   # click on empty background closes
            self.hide_overview()
        return True

    def on_pill_click(self, ev, ws):
        """Workspace strip: switch workspace and stay in the overview."""
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 1:
            ws.activate(self._server_time())     # 'active-workspace-changed' rebuilds
        return True

    def on_ws_click(self, ev, ws):
        """WORKSPACE_SCOPE == "all": click a workspace card to go there and close."""
        if event_in_tile(ev):
            return True
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 1:
            ts = self._server_time()
            self.hide_overview()
            ws.activate(ts)
        return True

    def on_tile_press(self, ev, w):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if ev.button == 1:
            return False             # let GTK detect a drag; activation is on release
        if ev.button == 2:
            self._close_window(w)
        elif ev.button == 3:
            self._tile_menu(w, ev)
        return True

    def on_tile_release(self, tile, ev, w):
        """Plain left click (no drag happened) = switch to the window."""
        if ev.button == 1 and not self.dragging:
            a = tile.get_allocation()
            if 0 <= ev.x < a.width and 0 <= ev.y < a.height:
                self._activate_window(w)
        return True

    # ---- drag & drop of windows between workspaces -------------------------
    def on_drag_begin(self, tile, ctx, w):
        self.dragging, self.drag_win = True, w
        tile.set_opacity(0.35)
        pb = self.snaps.cache.get(w.get_xid())
        if pb is None:
            pb = self.icon_for(w, 64)
        if pb is not None:
            pb = fit_pixbuf(pb, 240, 150)
            Gtk.drag_set_icon_pixbuf(ctx, pb, pb.get_width() // 2, pb.get_height() // 2)

    def on_drag_end(self, tile):
        self.dragging, self.drag_win = False, None
        try:
            tile.set_opacity(1.0)
        except Exception:
            pass
        if self._rb_pending:
            self._rebuild_soon(50)

    def can_drop_on(self, ws):
        w = self.drag_win
        if w is None:
            return False
        try:
            cur = w.get_workspace()
            return w.is_pinned() or cur is None or cur.get_number() != ws.get_number()
        except Exception:
            return True

    def on_window_dropped(self, data, ws):
        w = self.drag_win
        if w is None:                        # fall back to the xid carried in the drag data
            try:
                w = Wnck.Window.get(int(bytes(data.get_data()).decode()))
            except Exception:
                w = None
        if w is not None and self.can_drop_on(ws):
            self._move_window(w, ws)

    def _tile_menu(self, w, ev):
        menu = Gtk.Menu()
        cur = w.get_workspace()
        cur_n = cur.get_number() if cur is not None else -1
        for ws in self.screen.get_workspaces():
            if ws.get_number() == cur_n:
                continue
            mi = Gtk.MenuItem(label='Move to "%s"' % ws.get_name())
            mi.connect("activate", lambda _m, ws=ws: self._move_window(w, ws))
            menu.append(mi)
        if menu.get_children():
            menu.append(Gtk.SeparatorMenuItem())
        close = Gtk.MenuItem(label="Close window")
        close.connect("activate", lambda _m: self._close_window(w))
        menu.append(close)
        menu.show_all()
        self._menu = menu
        menu.popup_at_pointer(ev)

    def _move_window(self, w, ws):
        try:
            if w.is_pinned():                # "on all workspaces" would ignore the move
                w.unpin()
            w.move_to_workspace(ws)
        except Exception:
            pass
        self._rebuild_soon(250)

    def on_search_changed(self, panel, entry):
        if not self.is_open or panel is not self.main:
            return
        text = entry.get_text().strip()
        if not text:
            panel.stack.set_visible_child_name("windows")
            return
        self.results = self.apps.search(text)
        clear_children(panel.listbox)
        if not self.results:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(label="No applications found")
            add_class(lbl, "dim")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.add(lbl)
            panel.listbox.add(row)
        else:
            for app in self.results:
                panel.listbox.add(self._make_row(app))
        panel.listbox.show_all()
        panel.stack.set_visible_child_name("apps")
        first = panel.listbox.get_row_at_index(0)
        if first is not None and first.get_selectable():
            panel.listbox.select_row(first)

    def _make_row(self, app):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(6)
        gicon = app["info"].get_icon()
        if gicon is not None:
            img = Gtk.Image.new_from_gicon(gicon, Gtk.IconSize.DIALOG)
        else:
            img = Gtk.Image.new_from_icon_name("application-x-executable",
                                               Gtk.IconSize.DIALOG)
        img.set_pixel_size(40)
        box.pack_start(img, False, False, 0)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=app["name"])
        name.set_xalign(0)
        add_class(name, "app-name")
        col.pack_start(name, False, False, 0)
        desc_text = app["info"].get_description() or ""
        if desc_text:
            desc = Gtk.Label(label=desc_text)
            desc.set_xalign(0)
            desc.set_ellipsize(Pango.EllipsizeMode.END)
            desc.set_max_width_chars(60)
            add_class(desc, "app-desc")
            col.pack_start(desc, False, False, 0)
        box.pack_start(col, True, True, 0)
        row.add(box)
        return row

    def on_row_activated(self, row):
        i = row.get_index()
        if 0 <= i < len(self.results):
            self._launch(self.results[i])

    def on_entry_activate(self, panel):
        if not self.is_open or panel is not self.main:
            return
        if panel.stack.get_visible_child_name() == "apps":
            row = panel.listbox.get_selected_row()
            if row is not None and 0 <= row.get_index() < len(self.results):
                self._launch(self.results[row.get_index()])
        elif 0 <= self.sel < len(self.tiles):
            self._activate_window(self.tiles[self.sel][1])

    def on_key(self, panel, ev):
        k = ev.keyval
        if k == Gdk.KEY_Escape:
            self.hide_overview()
            return True
        if not self.is_open or self.main is None:
            return False
        if self.main.stack.get_visible_child_name() == "apps":
            if k in (Gdk.KEY_Down, Gdk.KEY_Tab):
                self._move_row(+1)
                return True
            if k in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
                self._move_row(-1)
                return True
            return False
        if self.main.entry.get_text():
            return False
        if k in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Tab):
            self._move_sel(+1)
            return True
        if k in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self._move_sel(-1)
            return True
        return False


def main():
    app = Overview()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
