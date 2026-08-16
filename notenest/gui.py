#!/usr/bin/env python3
r"""NoteNest -- an Aura (QuickOpen design system) GUI on top of the ``notenest`` API.

Layout per branding/aura-design-system/APP-LAYOUT-LANGUAGE.md, benchmarked
against Obsidian / Apple Notes (NoteNest is the layout language's reference
implementation):

  * **Sidebar** (AuraApp) -- Notes / About nav, plus a tag library in
    ``sidebar_body``: click a tag to filter, "All notes" to clear.  Collapsible
    with Ctrl+\.
  * **Toolbar** -- "+ New note" (primary), sort control, Write | Split | Read
    view switch, and the debounced search field (Ctrl+F).
  * **Content** -- a two-pane splitter: the note list (title + edited column,
    pinned notes on top with a star) and the editor pane (formatting bar,
    Markdown editor with autosave, live preview, and a Backlinks / Outgoing
    links panel).  Empty states show an Aura illustration instead of blank
    panes.
  * **Status bar** -- note count + live word count; errors surface here.

Daily-use features from the benchmark: pinned notes, rename with wiki-link
updating (F2), a Ctrl+P quick switcher, Ctrl+B/I/K formatting, Ctrl+E view
cycling, a Ctrl+, settings dialog (notebook, editor font size, System/Light/
Dark theme).  Fresh installs follow the OS Aura theme live.

Design goals baked in here (mirrors the QuickOpen house style):
  * built on the vendored ``notenest/aura.py`` design system (CustomTkinter).
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a note, returns 0) with no display or
    with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Every note operation calls the tested core library (vault/links/render/
    search); the search index is (re)built on a background thread and marshalled
    back with ``self.after``.  Failures show in the Aura status bar -- never a
    raw traceback.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading
import time

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (packaging, headless CI) never fails.

APP_NAME = "NoteNest"
APP_VERSION = "1.1.1"
WINDOW_TITLE = "NoteNest — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#5b86f7"      # Aura brand accent (owner-approved 2026-08-16; the old
                        # per-app green #17914b was a legacy scaffold accent)

VIEW_MODES = ("Write", "Split", "Read")
SORT_EDITED = "Recently edited"
SORT_TITLE = "Title A–Z"


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build.

    For a frozen exe we look only at ``sys._MEIPASS`` and the executable's own
    directory (never ``__file__``).  From source we also consult the package
    dir, the repo root and the CWD.  Returns an absolute path or ``None``.
    """
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def open_with_default_app(path):
    """Open a file/URL with the OS default application, guarded."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                # noqa: S606 - intended
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def rel_date(ts, now=None):
    """A compact human 'edited' stamp: 'now', '5m', '2h', 'Yesterday', '12 Aug'."""
    if not ts:
        return ""
    now = now if now is not None else time.time()
    diff = max(0, now - ts)
    if diff < 90:
        return "now"
    if diff < 3600:
        return "%dm" % (diff // 60)
    if diff < 86400 and time.localtime(ts).tm_mday == time.localtime(now).tm_mday:
        return "%dh" % (diff // 3600)
    if diff < 2 * 86400:
        return "Yesterday"
    st, sn = time.localtime(ts), time.localtime(now)
    if st.tm_year == sn.tm_year:
        return time.strftime("%d %b", st)
    return time.strftime("%b %Y", st)


def fuzzy_match(query, name):
    """True when *query* matches *name* (substring first, then subsequence)."""
    q = (query or "").casefold().strip()
    if not q:
        return True
    n = name.casefold()
    if q in n:
        return True
    it = iter(n)
    return all(ch in it for ch in q)


def word_count(text):
    return len((text or "").split())


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports.

    Kept inside a function so this module imports cleanly without a display
    (and without customtkinter installed).
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from html.parser import HTMLParser
    import customtkinter as ctk

    from . import aura, guiconfig, links, render, search, vault
    from .errors import NoteNestError

    # Readable families in both worlds; DejaVu is the Linux fallback so the
    # editor/preview never render as tofu under Xvfb.
    UI_FAMILY = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
    MONO_FAMILY = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"

    # (light, dark) palette pairs so CustomTkinter auto-flips these frames with
    # the theme (a single aura.P(...) value would freeze at build-time theme).
    pair = aura._pair

    # -- preview: render HTML into a styled tk.Text -----------------------
    class _PreviewParser(HTMLParser):
        """Turn the render.to_html fragment into styled Text-widget content.

        tkinter has no real HTML engine, so this maps a handful of block/inline
        tags to Text tags (headings, code, lists, emphasis) and records
        wiki-link spans so the app can make them clickable.
        """

        BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre",
                 "blockquote", "tr"}

        def __init__(self, widget, on_link):
            super().__init__(convert_charrefs=True)
            self.w = widget
            self.on_link = on_link
            self.style_stack = []
            self.link_target = None
            self._link_start = None
            self._pending_nl = 0
            self._list_depth = 0

        # -- helpers
        def _insert(self, text, extra=None):
            if not text:
                return
            tags = list(self.style_stack)
            if extra:
                tags.append(extra)
            self.w.insert("end", text, tuple(tags))

        def _newline(self, n=1):
            self._pending_nl = max(self._pending_nl, n)

        def _flush_nl(self):
            if self._pending_nl:
                self.w.insert("end", "\n" * self._pending_nl)
                self._pending_nl = 0

        # -- parser callbacks
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in self.BLOCK:
                self._newline(2 if tag.startswith("h") or tag in
                              ("p", "pre", "blockquote") else 1)
                self._flush_nl()
            if tag in ("h1", "h2", "h3"):
                self.style_stack.append(tag)
            elif tag in ("h4", "h5", "h6"):
                self.style_stack.append("h3")
            elif tag in ("strong", "b"):
                self.style_stack.append("bold")
            elif tag in ("em", "i"):
                self.style_stack.append("italic")
            elif tag == "code":
                self.style_stack.append("code")
            elif tag == "pre":
                self.style_stack.append("pre")
            elif tag == "blockquote":
                self.style_stack.append("quote")
            elif tag == "li":
                self._list_depth += 1
                self._insert("  " * self._list_depth + "• ")
            elif tag == "a":
                href = a.get("href", "")
                if href.startswith("note:"):
                    self.link_target = href[len("note:"):]
                    self._link_start = self.w.index("end-1c")
                    self.style_stack.append("link")

        def handle_endtag(self, tag):
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b",
                       "em", "i", "code", "pre", "blockquote"):
                if self.style_stack:
                    self.style_stack.pop()
                if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "pre",
                           "blockquote"):
                    self._newline(2)
            elif tag == "li":
                self._list_depth = max(0, self._list_depth - 1)
                self._newline(1)
            elif tag == "p":
                self._newline(2)
            elif tag == "a" and self.link_target is not None:
                end = self.w.index("end-1c")
                if self.style_stack and self.style_stack[-1] == "link":
                    self.style_stack.pop()
                self.on_link(self._link_start, end, self.link_target)
                self.link_target = None
                self._link_start = None

        def handle_data(self, data):
            if data.strip() == "" and "pre" not in self.style_stack:
                # collapse incidental whitespace between block tags
                if data and not data.strip("\n"):
                    return
            self._flush_nl()
            self._insert(data)

    # -- the main window --------------------------------------------------
    class App(aura.AuraApp):
        AUTOSAVE_MS = 700

        def __init__(self, notebook=None):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("note-nest.png"), version=APP_VERSION,
                tagline="offline notes",
                on_theme_change=guiconfig.set_theme,
                size=(1220, 740), min_size=(940, 560))

            self.root_dir = os.path.abspath(
                notebook or guiconfig.get_notebook()
                or guiconfig.default_notebook_dir())
            self._img_refs = []
            self._busy = False
            self._current = None        # current note name
            self._dirty = False
            self._autosave_job = None
            self._filter_job = None
            self._link_seq = 0
            self._active_tag = None     # sidebar tag filter (None = all)
            self._sort = SORT_EDITED
            self._view = "Split"
            self._mtimes = {}
            self._pins = []
            self._tag_rows = {}

            try:
                vault.ensure_notebook(self.root_dir)
            except NoteNestError:
                pass
            guiconfig.set_notebook(self.root_dir)
            self._pins = guiconfig.get_pins(self.root_dir)

            self._set_icon()
            self._build_menu()
            self.add_section("notes", "Notes", "✎", self._build_notes)
            self.add_section("about", "About", "ℹ", self._build_about)
            self._build_tag_sidebar()
            self.show("notes")
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._startup)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("note-nest.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("note-nest.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- menu + keyboard baseline (APP-LAYOUT-LANGUAGE.md §7/§9)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="New note", accelerator="Ctrl+N",
                              command=self._new_note)
            filem.add_command(label="Quick open…", accelerator="Ctrl+P",
                              command=self._quick_open)
            filem.add_command(label="Save", accelerator="Ctrl+S",
                              command=self._save_current)
            filem.add_separator()
            filem.add_command(label="Open notebook…", command=self._open_notebook)
            self._recent_menu = tk.Menu(filem, tearoff=0)
            filem.add_cascade(label="Recent notebooks", menu=self._recent_menu)
            filem.add_separator()
            filem.add_command(label="Reindex search", command=self._reindex)
            filem.add_command(label="Settings…", accelerator="Ctrl+,",
                              command=self._open_settings)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(label="Cycle Write / Split / Read",
                              accelerator="Ctrl+E", command=self._cycle_view)
            viewm.add_command(label="Toggle sidebar", accelerator="Ctrl+\\",
                              command=self.toggle_sidebar)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=lambda: self.show("about"))
            helpm.add_command(label="Open project page (quickopen.ai)",
                              command=lambda: open_with_default_app(PROJECT_URL))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

            self.bind_all("<Control-n>", lambda e: (self._new_note(), "break")[1])
            self.bind_all("<Control-s>", lambda e: (self._save_current(), "break")[1])
            self.bind_all("<Control-f>", lambda e: (self._focus_search(), "break")[1])
            self.bind_all("<Control-p>", lambda e: (self._quick_open(), "break")[1])
            self.bind_all("<Control-e>", lambda e: (self._cycle_view(), "break")[1])
            self.bind_all("<Control-comma>",
                          lambda e: (self._open_settings(), "break")[1])
            self.bind_all("<F2>", lambda e: self._rename_note())

        def _update_recent_menu(self):
            m = self._recent_menu
            try:
                m.delete(0, "end")
            except Exception:
                pass
            entries = [p for p in guiconfig.get_recent()
                       if os.path.abspath(p) != os.path.abspath(self.root_dir)]
            if not entries:
                m.add_command(label="(none yet)", state="disabled")
                return
            for p in entries[:8]:
                m.add_command(label=p,
                              command=lambda pp=p: self._switch_notebook(pp))

        # =================================================================
        # Sidebar tag library (sidebar_body)
        # =================================================================
        def _build_tag_sidebar(self):
            aura.SectionLabel(self.sidebar_body, "Tags").pack(
                anchor="w", padx=6, pady=(0, 4))
            self._tag_scroll = ctk.CTkScrollableFrame(
                self.sidebar_body, fg_color="transparent")
            self._tag_scroll.pack(fill="both", expand=True)

        def _tag_row(self, label, count, tag):
            active = (tag == self._active_tag)
            text = label if count is None else "%s   ·  %d" % (label, count)
            btn = ctk.CTkButton(
                self._tag_scroll, text=text, anchor="w", height=30,
                corner_radius=aura.TOKENS["geometry"]["radius_button"],
                fg_color=pair("accent_soft") if active else "transparent",
                hover_color=(aura._pal["light"]["surface2"],
                             aura._pal["dark"]["surface2"]),
                text_color=pair("text") if active else pair("muted"),
                font=aura.font(role="body"),
                command=lambda: self._set_tag_filter(tag))
            btn.pack(fill="x", pady=1)
            return btn

        def _refresh_tag_sidebar(self):
            for w in list(self._tag_scroll.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
            self._tag_rows.clear()
            try:
                counts = links.all_tags(self.root_dir)
            except NoteNestError:
                counts = {}
            self._tag_row("All notes", None, None)
            for t in sorted(counts, key=lambda t: (-counts[t], t.lower())):
                self._tag_row("#" + t, counts[t], t)
            # a stale filter (tag no longer present) falls back to All
            if self._active_tag and self._active_tag not in counts:
                self._active_tag = None

        def _set_tag_filter(self, tag):
            self._active_tag = tag
            self._refresh_tag_sidebar()
            self._refresh_notes()

        # =================================================================
        # Notes section — toolbar + splitter (list | editor)
        # =================================================================
        def _build_notes(self, frame):
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)

            # ---- toolbar (primary action left; view + search right)
            tb = aura.Toolbar(frame)
            tb.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            tb.add_button("＋ New note", self._new_note, kind="primary")
            self.search = tb.add_search("Search notes…  (Ctrl+F)",
                                        on_change=lambda _t: self._refresh_notes(),
                                        width=250)
            self.sort_opt = aura.AuraOption(
                tb, values=[SORT_EDITED, SORT_TITLE], width=150, height=32,
                command=self._set_sort)
            self.sort_opt.set(self._sort)
            tb.add_right(self.sort_opt)
            self.view_seg = aura.SegmentedControl(
                tb, values=list(VIEW_MODES), width=216, command=self._set_view)
            self.view_seg.set(self._view)
            tb.add_right(self.view_seg)

            # ---- the workspace splitter
            self.panes = ttk.Panedwindow(frame, orient="horizontal")
            self.panes.grid(row=1, column=0, sticky="nsew")

            # ---- left: note list (title + edited, pins on top)
            listwrap = ctk.CTkFrame(self.panes, fg_color=pair("surface"),
                                    corner_radius=10, border_width=1,
                                    border_color=pair("border"))
            self.panes.add(listwrap, weight=1)
            self.tree = ttk.Treeview(listwrap, columns=("title", "edited"),
                                     show="headings", selectmode="browse")
            self.tree.heading("title", text="Note",
                              command=lambda: self._set_sort(SORT_TITLE))
            self.tree.heading("edited", text="Edited",
                              command=lambda: self._set_sort(SORT_EDITED))
            self.tree.column("title", width=190, stretch=True)
            self.tree.column("edited", width=70, minwidth=64, stretch=False,
                             anchor="e")
            tsb = aura.AuraScrollbar(listwrap, command=self.tree.yview)
            self.tree.configure(yscrollcommand=tsb.set)
            tsb.pack(side="right", fill="y", padx=(0, 4), pady=6)
            self.tree.pack(side="left", fill="both", expand=True,
                           padx=(6, 0), pady=6)
            self.tree.bind("<<TreeviewSelect>>", self._on_note_select)
            self.tree.bind("<Delete>", lambda e: self._delete_note())
            self.tree.bind("<Button-3>", self._show_note_menu)

            self._note_menu = tk.Menu(self, tearoff=0)
            aura.track(self._note_menu, "menu")

            # ---- right: editor pane
            right = ctk.CTkFrame(self.panes, fg_color=pair("bg"),
                                 corner_radius=0)
            self.panes.add(right, weight=3)
            right.grid_columnconfigure(0, weight=1)
            right.grid_rowconfigure(1, weight=1)

            head = ctk.CTkFrame(right, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=(10, 0), pady=(0, 6))
            head.grid_columnconfigure(0, weight=1)
            twrap = ctk.CTkFrame(head, fg_color="transparent")
            twrap.grid(row=0, column=0, sticky="w")
            self.title_lbl = aura.Heading(twrap, "")
            self.title_lbl.pack(side="left")
            self.edited_lbl = aura.Caption(twrap, "")
            self.edited_lbl.pack(side="left", padx=(10, 0))
            fmt = ctk.CTkFrame(head, fg_color="transparent")
            fmt.grid(row=0, column=1, sticky="e")
            self.pin_btn = self._fmt_btn(fmt, "☆", self._toggle_pin,
                                         "Pin note (keeps it on top)")
            self._fmt_btn(fmt, "B", lambda: self._wrap_sel("**"),
                          "Bold (Ctrl+B)", bold=True)
            self._fmt_btn(fmt, "I", lambda: self._wrap_sel("*"),
                          "Italic (Ctrl+I)", italic=True)
            self._fmt_btn(fmt, "‹›", lambda: self._wrap_sel("`"),
                          "Inline code")
            self._fmt_btn(fmt, "[[ ]]", self._insert_link,
                          "Wiki-link (Ctrl+K)")
            self._fmt_btn(fmt, "•", self._toggle_bullet, "Bullet list")

            # editor | preview inner splitter
            self.ed_panes = ttk.Panedwindow(right, orient="horizontal")
            self.ed_panes.grid(row=1, column=0, sticky="nsew", padx=(10, 0))

            self.ed_wrap = ctk.CTkFrame(self.ed_panes, fg_color=pair("field"),
                                        corner_radius=10, border_width=1,
                                        border_color=pair("border"))
            self.editor = tk.Text(self.ed_wrap, wrap="word", undo=True,
                                  relief="flat", padx=10, pady=8,
                                  font=(MONO_FAMILY, guiconfig.get_font_size()))
            esb = aura.AuraScrollbar(self.ed_wrap, command=self.editor.yview)
            self.editor.configure(yscrollcommand=esb.set)
            esb.pack(side="right", fill="y", padx=(0, 4), pady=4)
            self.editor.pack(side="left", fill="both", expand=True,
                             padx=(4, 0), pady=4)
            aura.track(self.editor, "text")
            self.editor.bind("<<Modified>>", self._on_editor_modified)
            self.editor.bind("<Control-b>",
                             lambda e: (self._wrap_sel("**"), "break")[1])
            self.editor.bind("<Control-i>",
                             lambda e: (self._wrap_sel("*"), "break")[1])
            self.editor.bind("<Control-k>",
                             lambda e: (self._insert_link(), "break")[1])

            self.pv_wrap = ctk.CTkFrame(self.ed_panes, fg_color=pair("field"),
                                        corner_radius=10, border_width=1,
                                        border_color=pair("border"))
            self.preview = tk.Text(self.pv_wrap, wrap="word", state="disabled",
                                   cursor="arrow", relief="flat",
                                   padx=10, pady=8, font=(UI_FAMILY, 11))
            psb = aura.AuraScrollbar(self.pv_wrap, command=self.preview.yview)
            self.preview.configure(yscrollcommand=psb.set)
            psb.pack(side="right", fill="y", padx=(0, 4), pady=4)
            self.preview.pack(side="left", fill="both", expand=True,
                              padx=(4, 0), pady=4)
            aura.track(self.preview, "text")
            self._configure_preview_tags(self.preview)

            # backlinks / outgoing links panel
            self.link_tabs = ttk.Notebook(right, height=150)
            self.link_tabs.grid(row=2, column=0, sticky="ew", padx=(10, 0),
                                pady=(8, 0))
            bl_frame = ttk.Frame(self.link_tabs)
            self.backlinks_list = tk.Listbox(bl_frame, activestyle="none",
                                             exportselection=False, height=5,
                                             font=(UI_FAMILY, 10))
            self.backlinks_list.pack(fill="both", expand=True)
            self.backlinks_list.bind(
                "<Double-Button-1>",
                lambda e: self._open_from_list(self.backlinks_list))
            aura.track(self.backlinks_list, "listbox")
            self.link_tabs.add(bl_frame, text=aura.spaced("Backlinks"))

            out_frame = ttk.Frame(self.link_tabs)
            self.links_list = tk.Listbox(out_frame, activestyle="none",
                                         exportselection=False, height=5,
                                         font=(UI_FAMILY, 10))
            self.links_list.pack(fill="both", expand=True)
            self.links_list.bind(
                "<Double-Button-1>",
                lambda e: self._open_from_list(self.links_list))
            aura.track(self.links_list, "listbox")
            self.link_tabs.add(out_frame, text=aura.spaced("Outgoing links"))

            # ---- empty states
            self.empty_all = aura.EmptyState(
                frame, title="No notes yet",
                caption="Your notebook is empty. Write your first Markdown "
                        "note — link notes with [[wiki-links]] and tag them "
                        "with #tags.",
                action_text="＋ New note", action=self._new_note,
                image=(asset_path("assets/notes-empty-light.png"),
                       asset_path("assets/notes-empty-dark.png")))
            self.empty_note = aura.EmptyState(
                right, glyph="✎", title="No note selected",
                caption="Choose a note on the left, press Ctrl+P to quick-open, "
                        "or Ctrl+N to create one.")
            self._apply_view()
            self.after(250, self._init_sashes)

        def _init_sashes(self):
            """Give the splitters their commercial-app proportions once the
            window is mapped (a Panedwindow starts at requested sizes, which
            squeezes the note list to a sliver otherwise)."""
            try:
                if self.panes.winfo_width() > 700:
                    self.panes.sashpos(0, 320)
            except Exception:
                pass
            self._center_split()

        def _center_split(self):
            try:
                if self._view == "Split":
                    w = self.ed_panes.winfo_width()
                    if w > 400:
                        self.ed_panes.sashpos(0, w // 2)
            except Exception:
                pass

        def _fmt_btn(self, parent, text, command, tip, bold=False, italic=False):
            f = aura.font(11, "bold" if bold else "normal")
            if italic:
                f = ctk.CTkFont(family=f.cget("family"), size=11, slant="italic")
            btn = ctk.CTkButton(parent, text=text, width=34, height=28,
                                corner_radius=8, fg_color="transparent",
                                hover_color=pair("accent_soft"),
                                text_color=pair("muted"), font=f,
                                command=command)
            btn.pack(side="left", padx=1)
            aura.Tooltip(btn, tip)
            return btn

        # ---- view modes (Write | Split | Read)
        def _set_view(self, mode):
            if mode in VIEW_MODES:
                self._view = mode
            self._apply_view()

        def _cycle_view(self):
            i = VIEW_MODES.index(self._view) if self._view in VIEW_MODES else 1
            self._view = VIEW_MODES[(i + 1) % len(VIEW_MODES)]
            try:
                self.view_seg.set(self._view)
            except Exception:
                pass
            self._apply_view()

        def _apply_view(self):
            try:
                for w in (self.ed_wrap, self.pv_wrap):
                    try:
                        self.ed_panes.forget(w)
                    except Exception:
                        pass
                if self._view in ("Write", "Split"):
                    self.ed_panes.add(self.ed_wrap, weight=1)
                if self._view in ("Read", "Split"):
                    self.ed_panes.add(self.pv_wrap, weight=1)
            except Exception:
                pass
            if self._view != "Write":
                self._refresh_preview()
            self.after(60, self._center_split)

        # ---- notebook label / empty-state plumbing
        def _update_empty_states(self):
            has_any = bool(vault.list_notes(self.root_dir))
            if has_any:
                self.empty_all.place_forget()
                self.panes.grid()
            else:
                self.panes.grid_remove()
                self.empty_all.place(relx=0, rely=0.08, relwidth=1,
                                     relheight=0.92)
            if self._current is None:
                self.empty_note.place(x=0, y=0, relwidth=1, relheight=1)
                self.empty_note.lift()
            else:
                self.empty_note.place_forget()

        # ---- preview tag styling (re-run on theme flip)
        def _configure_preview_tags(self, w):
            w.tag_configure("h1", font=(UI_FAMILY, 18, "bold"), spacing1=6, spacing3=4)
            w.tag_configure("h2", font=(UI_FAMILY, 15, "bold"), spacing1=5, spacing3=3)
            w.tag_configure("h3", font=(UI_FAMILY, 13, "bold"), spacing1=4, spacing3=2)
            w.tag_configure("bold", font=(UI_FAMILY, 11, "bold"))
            w.tag_configure("italic", font=(UI_FAMILY, 11, "italic"))
            w.tag_configure("code", font=(MONO_FAMILY, 10),
                            background=aura.P("surface3"))
            w.tag_configure("pre", font=(MONO_FAMILY, 10),
                            background=aura.P("surface3"), lmargin1=12, lmargin2=12)
            w.tag_configure("quote", foreground=aura.P("muted"),
                            lmargin1=12, lmargin2=12)
            w.tag_configure("link", foreground=aura.P("accent"), underline=1)

        # ---- theme: keep the raw-tk preview tags + rendering in sync
        def set_theme(self, theme, _system=False):
            super().set_theme(theme, _system=_system)
            try:
                self._configure_preview_tags(self.preview)
                self._refresh_preview()
                self._style_tree_tags()
                self._refresh_tag_sidebar()
            except Exception:
                pass

        def _style_tree_tags(self):
            try:
                self.tree.tag_configure("pinned", foreground=aura.P("accent"))
            except Exception:
                pass

        # ---- startup
        def _startup(self):
            self._style_tree_tags()
            self._refresh_tag_sidebar()
            self._refresh_notes()
            self._auto_select_first()
            self._update_recent_menu()
            self._reindex(quiet=True)
            self.after(60, self._focus_search)

        def _auto_select_first(self):
            """Open where you left off: select the most recent note."""
            if self._current is None:
                children = self.tree.get_children()
                if children:
                    self._select_note(children[0])

        def _focus_search(self):
            try:
                self.show("notes")
                self.search.focus_set()
            except Exception:
                pass

        # ---- background runner (threaded; marshalled back with self.after)
        def _bg(self, work, on_ok, busy="Working…"):
            if self._busy:
                return
            self._busy = True
            self.set_status(busy, kind="working")

            def run():
                try:
                    res, err = work(), None
                except NoteNestError as ex:
                    res, err = None, str(ex)
                except Exception as ex:
                    res, err = None, f"Unexpected error: {ex}"
                try:
                    self.after(0, lambda: finish(res, err))
                except Exception:       # window already destroyed
                    self._busy = False

            def finish(res, err):
                self._busy = False
                if err is not None:
                    self.set_error(err)
                    return
                try:
                    on_ok(res)
                except Exception as ex:
                    self.set_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- notebook switching
        def _open_notebook(self):
            d = filedialog.askdirectory(title="Choose a notebook folder")
            if d:
                self._switch_notebook(d)

        def _switch_notebook(self, d):
            self._save_current()
            root = os.path.abspath(d)
            try:
                vault.ensure_notebook(root)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self.root_dir = root
            guiconfig.set_notebook(root)
            self._pins = guiconfig.get_pins(root)
            self._current = None
            self._active_tag = None
            self.editor.delete("1.0", "end")
            self.title_lbl.configure(text="")
            self.edited_lbl.configure(text="")
            self._clear_preview()
            self._refresh_tag_sidebar()
            self._refresh_notes()
            self._auto_select_first()
            self._update_recent_menu()
            self._reindex(quiet=True)
            self.set_success(f"Notebook: {self.root_dir}")

        # ---- note list
        def _visible_notes(self):
            query = self.search.get().strip() if hasattr(self, "search") else ""
            if query:
                try:
                    names = [h.name for h in search.search(self.root_dir, query)]
                except NoteNestError:
                    names = vault.list_notes(self.root_dir)
            else:
                names = vault.list_notes(self.root_dir)
            if self._active_tag:
                tag = self._active_tag.lower()
                filtered = []
                for n in names:
                    try:
                        if tag in [t.lower() for t in
                                   links.parse_tags(vault.read_note(self.root_dir, n))]:
                            filtered.append(n)
                    except NoteNestError:
                        continue
                names = filtered
            return names

        def _order_notes(self, names):
            self._mtimes = {n: vault.note_mtime(self.root_dir, n) or 0
                            for n in names}
            searching = bool(self.search.get().strip()) if hasattr(self, "search") else False
            if not searching:            # ranked search keeps relevance order
                if self._sort == SORT_TITLE:
                    names = sorted(names, key=lambda n: n.lower())
                else:
                    names = sorted(names, key=lambda n: -self._mtimes[n])
            pinned = [n for n in self._pins if n in names]
            rest = [n for n in names if n not in pinned]
            return pinned + rest

        def _refresh_notes(self):
            names = self._order_notes(self._visible_notes())
            now = time.time()
            self.tree.delete(*self.tree.get_children())
            for n in names:
                pin = n in self._pins
                self.tree.insert(
                    "", "end", iid=n,
                    values=(("★ " if pin else "") + n,
                            rel_date(self._mtimes.get(n), now)),
                    tags=("pinned",) if pin else ())
            if self._current in names:
                self.tree.selection_set(self._current)
                self.tree.see(self._current)
            self._update_empty_states()
            self._update_counts()

        def _update_counts(self):
            n = len(self.tree.get_children()) if hasattr(self, "tree") else 0
            msg = f"{n} note(s)"
            if self._current is not None:
                words = word_count(self.editor.get("1.0", "end-1c"))
                msg += f"   ·   {words} words"
            self.set_status(msg)

        def _set_sort(self, value):
            self._sort = value if value in (SORT_EDITED, SORT_TITLE) else SORT_EDITED
            try:
                self.sort_opt.set(self._sort)
            except Exception:
                pass
            self._refresh_notes()

        def _on_note_select(self, _e=None):
            sel = self.tree.selection()
            if not sel:
                return
            name = sel[0]
            if name == self._current:
                return
            self._save_current()
            self._load_note(name)

        def _load_note(self, name):
            try:
                text = vault.read_note(self.root_dir, name)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self._current = name
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", text)
            self.editor.edit_reset()
            self.editor.edit_modified(False)
            self._dirty = False
            self.title_lbl.configure(text=name)
            ts = vault.note_mtime(self.root_dir, name)
            self.edited_lbl.configure(
                text=("edited " + rel_date(ts)) if ts else "")
            self._update_pin_button()
            self._refresh_preview()
            self._refresh_side_panels()
            self._update_empty_states()
            self._update_counts()

        # ---- editing / autosave
        def _on_editor_modified(self, _e=None):
            if not self.editor.edit_modified():
                return
            self.editor.edit_modified(False)
            if self._current is None:
                return
            self._dirty = True
            if self._autosave_job:
                self.after_cancel(self._autosave_job)
            self._autosave_job = self.after(self.AUTOSAVE_MS, self._autosave)
            self._update_counts()
            if self._view != "Write":
                self._refresh_preview()

        def _autosave(self):
            self._autosave_job = None
            self._save_current()

        def _save_current(self):
            if self._current is None or not self._dirty:
                return
            text = self.editor.get("1.0", "end-1c")
            try:
                vault.write_note(self.root_dir, self._current, text)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self._dirty = False
            self.set_success(f"Saved {self._current}")
            # incremental index update + side panels off the UI thread
            name = self._current
            self._bg(lambda: search.update_note(self.root_dir, name),
                     lambda _r: self._update_counts(), busy="Indexing…")
            self._refresh_tag_sidebar()
            self._refresh_side_panels()

        # ---- small modal prompt (house Dialog convention)
        def _ask_string(self, title, label, initial=""):
            result = {"value": None}
            dlg = aura.Dialog(self, title=title, size=(420, 190))
            aura.Caption(dlg.body, label).pack(anchor="w")
            entry = aura.AuraEntry(dlg.body)
            entry.pack(fill="x", pady=(6, 0))
            if initial:
                entry.insert(0, initial)

            def ok(_e=None):
                result["value"] = entry.get().strip()
                dlg.close()

            dlg.add_button("OK", ok)
            dlg.add_button("Cancel", dlg.close, kind="secondary")
            entry.bind("<Return>", ok)
            self.after(120, entry.focus_set)
            self.wait_window(dlg)
            return result["value"] or None

        def _new_note(self):
            title = self._ask_string("New note", "Note title:")
            if not title:
                return
            if vault.note_exists(self.root_dir, title):
                self.set_error(f"A note named {title!r} already exists.")
                self._select_note(title)
                return
            try:
                vault.write_note(self.root_dir, title, f"# {title}\n\n")
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self._bg(lambda: search.update_note(self.root_dir, title),
                     lambda _r: self._update_counts(), busy="Indexing…")
            self.search.set("")
            self._active_tag = None
            self._refresh_tag_sidebar()
            self._refresh_notes()
            self._select_note(title)
            self.set_success(f"Created {title}")
            self.editor.focus_set()

        def _rename_note(self):
            if self._current is None:
                return
            old = self._current
            new = self._ask_string("Rename note", "New name:", initial=old)
            if not new or new == old:
                return
            self._save_current()
            try:
                vault.rename(self.root_dir, old, new)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            updated = self._update_links_to(old, new)
            guiconfig.rename_pin(self.root_dir, old, new)
            self._pins = guiconfig.get_pins(self.root_dir)
            self._current = new

            def work():
                search.remove_note(self.root_dir, old)
                search.update_note(self.root_dir, new)
            self._bg(work, lambda _r: self._update_counts(), busy="Indexing…")
            self._refresh_notes()
            self._select_note(new)
            extra = f" · updated links in {updated} note(s)" if updated else ""
            self.set_success(f"Renamed to {new}{extra}")

        def _update_links_to(self, old, new):
            """Rewrite [[old]] / [[old|label]] in other notes to point at *new*.

            The benchmark behavior (Obsidian keeps links working on rename).
            Only exact-target matches (case-insensitive, full name or title)
            are rewritten; returns the number of notes touched.
            """
            import re
            count = 0
            old_l = old.lower()
            old_title = old.split("/")[-1].lower()

            def repl(m):
                inner = m.group(1)
                target, _, label = inner.partition("|")
                t = target.strip()
                if t.lower() in (old_l, old_title):
                    return "[[%s%s]]" % (new, ("|" + label) if label else "")
                return m.group(0)

            pat = re.compile(r"\[\[([^\[\]]+?)\]\]")
            for name in vault.list_notes(self.root_dir):
                if name == new:
                    continue
                try:
                    text = vault.read_note(self.root_dir, name)
                    new_text = pat.sub(repl, text)
                    if new_text != text:
                        vault.write_note(self.root_dir, name, new_text)
                        count += 1
                except NoteNestError:
                    continue
            return count

        def _delete_note(self):
            sel = self.tree.selection()
            name = sel[0] if sel else self._current
            if name is None:
                self.set_status("Select a note to delete.")
                return
            if not messagebox.askyesno("Delete note",
                                       f"Delete '{name}'? This cannot be undone."):
                return
            try:
                vault.delete_note(self.root_dir, name)
                search.remove_note(self.root_dir, name)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            guiconfig.set_pinned(self.root_dir, name, False)
            self._pins = guiconfig.get_pins(self.root_dir)
            if name == self._current:
                self._current = None
                self._dirty = False
                self.editor.delete("1.0", "end")
                self.title_lbl.configure(text="")
                self.edited_lbl.configure(text="")
                self._clear_preview()
            self._refresh_tag_sidebar()
            self._refresh_notes()
            self.set_success(f"Deleted {name}")

        # ---- pinning
        def _toggle_pin(self, name=None):
            name = name or self._current
            if name is None:
                return
            pinned = name not in self._pins
            guiconfig.set_pinned(self.root_dir, name, pinned)
            self._pins = guiconfig.get_pins(self.root_dir)
            self._update_pin_button()
            self._refresh_notes()
            self.set_status(("Pinned " if pinned else "Unpinned ") + name)

        def _update_pin_button(self):
            try:
                pinned = self._current in self._pins
                self.pin_btn.configure(
                    text="★" if pinned else "☆",
                    text_color=aura.P("accent") if pinned else aura.P("muted"))
            except Exception:
                pass

        # ---- context menu
        def _show_note_menu(self, event):
            iid = self.tree.identify_row(event.y)
            if not iid:
                return
            self.tree.selection_set(iid)
            m = self._note_menu
            m.delete(0, "end")
            pinned = iid in self._pins
            m.add_command(label="Open", command=lambda: self._select_note(iid))
            m.add_command(label=("Unpin" if pinned else "Pin"),
                          command=lambda: self._toggle_pin(iid))
            m.add_command(label="Rename…  (F2)",
                          command=lambda: (self._select_note(iid),
                                           self._rename_note()))
            m.add_separator()
            m.add_command(label="Delete…", command=self._delete_note)
            aura.style_menu(m)
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    m.grab_release()
                except Exception:
                    pass

        def _select_note(self, name):
            if name in self.tree.get_children():
                self.tree.selection_set(name)
                self.tree.see(name)
                if name != self._current:
                    self._load_note(name)

        # ---- quick switcher (Ctrl+P)
        def _quick_open(self):
            names = self._order_notes(vault.list_notes(self.root_dir))
            if not names:
                self.set_status("No notes yet — press Ctrl+N to create one.")
                return
            dlg = aura.Dialog(self, title="Quick open", size=(520, 420))
            box = tk.Listbox(dlg.body, activestyle="none",
                             exportselection=False,
                             font=(UI_FAMILY, 11))
            se = aura.SearchEntry(dlg.body, placeholder="Type a note name…",
                                  delay=80)
            se.pack(fill="x")
            box.pack(fill="both", expand=True, pady=(8, 0))
            aura.track(box, "listbox")

            def refill(_t=None):
                q = se.get()
                box.delete(0, "end")
                for n in names:
                    if fuzzy_match(q, n):
                        box.insert("end", n)
                if box.size():
                    box.selection_set(0)
            se.on_change = refill
            refill()

            def choose(_e=None):
                sel = box.curselection()
                if sel:
                    name = box.get(sel[0])
                    dlg.close()
                    self.show("notes")
                    self._select_note(name)

            def move(delta):
                if not box.size():
                    return
                cur = box.curselection()
                i = (cur[0] if cur else 0) + delta
                i = max(0, min(box.size() - 1, i))
                box.selection_clear(0, "end")
                box.selection_set(i)
                box.see(i)

            box.bind("<Double-Button-1>", choose)
            box.bind("<Return>", choose)
            for w in (se.entry, dlg):
                w.bind("<Return>", choose)
                w.bind("<Down>", lambda e: move(1))
                w.bind("<Up>", lambda e: move(-1))
            self.after(120, se.focus_set)

        # ---- settings (Ctrl+,)
        def _open_settings(self):
            dlg = aura.Dialog(self, title="Settings", size=(540, 420))

            aura.SectionLabel(dlg.body, "Notebook").pack(anchor="w", pady=(0, 2))
            nb_cap = aura.Caption(dlg.body, self.root_dir)
            nb_cap.pack(anchor="w")
            row = ctk.CTkFrame(dlg.body, fg_color="transparent")
            row.pack(anchor="w", pady=(6, 14))

            def change_nb():
                d = filedialog.askdirectory(title="Choose a notebook folder",
                                            parent=dlg)
                if d:
                    self._switch_notebook(d)
                    nb_cap.configure(text=self.root_dir)
            aura.AuraButton(row, "Change…", kind="secondary", height=30,
                            command=change_nb).pack(side="left")
            aura.AuraButton(row, "Open folder", kind="ghost", height=30,
                            command=lambda: open_with_default_app(
                                self.root_dir)).pack(side="left", padx=(8, 0))

            aura.SectionLabel(dlg.body, "Editor").pack(anchor="w", pady=(0, 2))
            frow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            frow.pack(anchor="w", pady=(4, 14))
            aura.Caption(frow, "Font size").pack(side="left", padx=(0, 10))
            fs = aura.AuraOption(
                frow, values=[str(s) for s in guiconfig.FONT_SIZES],
                width=90, height=30, command=self._set_font_size)
            fs.set(str(guiconfig.get_font_size()))
            fs.pack(side="left")

            aura.SectionLabel(dlg.body, "Appearance").pack(anchor="w", pady=(0, 2))
            trow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            trow.pack(anchor="w", pady=(4, 0))
            aura.Caption(trow, "Theme").pack(side="left", padx=(0, 10))
            cur = guiconfig.get_theme()
            th = aura.AuraOption(trow, values=["System", "Light", "Dark"],
                                 width=110, height=30,
                                 command=self._set_theme_pref)
            th.set(cur.capitalize() if cur in ("light", "dark") else "System")
            th.pack(side="left")
            aura.Caption(dlg.body,
                         "System follows the OS Aura Dark/Light live.").pack(
                anchor="w", pady=(6, 0))

            dlg.add_button("Close")

        def _set_font_size(self, value):
            try:
                size = int(value)
            except (TypeError, ValueError):
                return
            guiconfig.set_font_size(size)
            try:
                self.editor.configure(font=(MONO_FAMILY, size))
            except Exception:
                pass

        def _set_theme_pref(self, choice):
            pref = str(choice).lower()
            if pref == "system":
                guiconfig.set_theme("system")
                self._follow_system = True
                if self._sys_listener is None:
                    self._start_system_listener()
                self.set_theme(aura._system_theme(), _system=True)
            elif pref in ("light", "dark"):
                self.set_theme(pref)     # persists via on_theme_change

        # ---- preview
        def _clear_preview(self):
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self.preview.configure(state="disabled")

        def _refresh_preview(self):
            if self._current is None:
                self._clear_preview()
                return
            text = self.editor.get("1.0", "end-1c")
            try:
                htmlfrag = render.to_html(text)
            except Exception:
                htmlfrag = ""
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self._configure_preview_tags(self.preview)
            try:
                parser = _PreviewParser(self.preview, self._add_preview_link)
                parser.feed(htmlfrag)
                parser.close()
            except Exception:
                self.preview.insert("1.0", text)
            self.preview.configure(state="disabled")

        def _add_preview_link(self, start, end, target):
            self._link_seq += 1
            tag = f"lnk-{self._link_seq}"
            self.preview.tag_add(tag, start, end)
            self.preview.tag_configure(tag, foreground=aura.P("accent"),
                                       underline=1)
            self.preview.tag_bind(tag, "<Button-1>",
                                  lambda e, t=target: self._follow_link(t))
            self.preview.tag_bind(tag, "<Enter>",
                                  lambda e: self.preview.configure(cursor="hand2"))
            self.preview.tag_bind(tag, "<Leave>",
                                  lambda e: self.preview.configure(cursor="arrow"))

        def _follow_link(self, target):
            self._save_current()
            try:
                name = links.resolve_link(self.root_dir, target, create=True)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            if not name:
                self.set_error(f"Could not resolve link: {target}")
                return
            self._bg(lambda: search.update_note(self.root_dir, name),
                     lambda _r: self._update_counts(), busy="Indexing…")
            self.search.set("")
            self._refresh_tag_sidebar()
            self._refresh_notes()
            self._select_note(name)

        def _refresh_side_panels(self):
            self.backlinks_list.delete(0, "end")
            self.links_list.delete(0, "end")
            nbl = nol = 0
            if self._current is not None:
                try:
                    for n in links.backlinks(self.root_dir, self._current):
                        self.backlinks_list.insert("end", n)
                        nbl += 1
                    text = vault.read_note(self.root_dir, self._current)
                    for target in links.parse_links(text):
                        resolved = links.resolve_link(self.root_dir, target)
                        label = target if resolved else target + "  (new)"
                        self.links_list.insert("end", label)
                        nol += 1
                except NoteNestError:
                    pass
            try:
                self.link_tabs.tab(0, text=aura.spaced(f"Backlinks ({nbl})"))
                self.link_tabs.tab(1, text=aura.spaced(f"Outgoing ({nol})"))
            except Exception:
                pass

        def _open_from_list(self, listbox):
            sel = listbox.curselection()
            if not sel:
                return
            name = listbox.get(sel[0]).replace("  (new)", "").strip()
            self._follow_link(name)

        # ---- editor formatting helpers
        def _wrap_sel(self, marker):
            if self._current is None:
                return
            ed = self.editor
            try:
                start = ed.index("sel.first")
                end = ed.index("sel.last")
                text = ed.get(start, end)
                if text.startswith(marker) and text.endswith(marker) and \
                        len(text) >= 2 * len(marker):
                    new = text[len(marker):-len(marker)]
                else:
                    new = marker + text + marker
                ed.delete(start, end)
                ed.insert(start, new)
                ed.tag_add("sel", start, f"{start}+{len(new)}c")
            except tk.TclError:      # no selection: insert markers, cursor mid
                ed.insert("insert", marker + marker)
                ed.mark_set("insert", f"insert-{len(marker)}c")
            ed.focus_set()

        def _insert_link(self):
            if self._current is None:
                return
            ed = self.editor
            try:
                start = ed.index("sel.first")
                end = ed.index("sel.last")
                text = ed.get(start, end)
                ed.delete(start, end)
                ed.insert(start, f"[[{text}]]")
            except tk.TclError:
                ed.insert("insert", "[[]]")
                ed.mark_set("insert", "insert-2c")
            ed.focus_set()

        def _toggle_bullet(self):
            if self._current is None:
                return
            ed = self.editor
            line_start = ed.index("insert linestart")
            line = ed.get(line_start, ed.index("insert lineend"))
            if line.lstrip().startswith("- "):
                idx = line.index("- ")
                ed.delete(f"{line_start}+{idx}c", f"{line_start}+{idx + 2}c")
            else:
                ed.insert(line_start, "- ")
            ed.focus_set()

        # ---- reindex
        def _reindex(self, quiet=False):
            def done(n):
                if not quiet:
                    self.set_success(f"Reindexed {n} note(s).")
                else:
                    self._update_counts()
            self._bg(lambda: search.reindex(self.root_dir), done, busy="Indexing…")

        # =================================================================
        # About section
        # =================================================================
        def _build_about(self, frame):
            card = aura.Card(frame, title="About NoteNest")
            card.pack(fill="x")
            aura.Heading(card.body, APP_NAME).pack(anchor="w")
            aura.Caption(card.body, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                card.body, font=aura.font(), justify="left", anchor="w",
                wraplength=560,
                text="A fast, fully-offline Markdown knowledge base — "
                     "wiki-links, backlinks, tags, pinned notes and instant "
                     "full-text search over plain files you own.\n\n"
                     "100% AI-built, open source, published on QuickOpen. "
                     "Nothing is ever uploaded anywhere.").pack(anchor="w")
            aura.Caption(card.body,
                         "Shortcuts: Ctrl+N new · Ctrl+P quick open · Ctrl+F "
                         "search · Ctrl+E view · Ctrl+B/I/K format · F2 rename "
                         "· Ctrl+, settings · Ctrl+\\ sidebar").pack(
                anchor="w", pady=(10, 0))
            aura.Caption(card.body,
                         "Licensed under Apache-2.0. Built on python-markdown "
                         "and Whoosh (BSD), and CustomTkinter (MIT).").pack(
                anchor="w", pady=(10, 4))
            aura.AuraButton(card.body, "Project page: quickopen.ai",
                            kind="ghost",
                            command=lambda: open_with_default_app(
                                PROJECT_URL)).pack(anchor="w", pady=(6, 0))

        # ---- shutdown
        def _on_close(self):
            try:
                self._save_current()
            except Exception:
                pass
            self.destroy()

    return App


def main():
    """Entry point: build the root window and run.  Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising, so headless
    callers stay clean.
    """
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
