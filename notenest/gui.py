#!/usr/bin/env python3
r"""NoteNest -- a pure-stdlib tkinter GUI on top of the ``notenest`` API.

A single main window with three panes:

  * **left**  -- a search box, a tag filter and the note list, plus New/Delete;
  * **centre** -- the Markdown editor (autosaves as you type);
  * **right** -- a live, readable preview of the rendered Markdown, and a tabbed
    panel of backlinks + outgoing links.  Wiki-links in the preview are
    clickable and navigate (creating the target note if it does not exist yet).

Design goals mirror the QuickOpen house style:
  * pure standard-library tkinter/ttk -- NO third-party GUI deps.  Dark mode is
    a ttk-style + palette swap.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a note, returns 0) with no display.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Every note operation calls the tested core library (vault/links/render/
    search); the search index is (re)built on a background thread and marshalled
    back with ``self.after``.  Failures show inline -- never a raw traceback.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter is imported lazily inside main()/build_app so that merely
# importing this module (packaging, headless CI) never fails.

APP_NAME = "NoteNest"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "NoteNest — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"

# ---- colour palettes (mirror the QuickOpen palette) -------------------------
PALETTES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "text": "#141820",
        "muted": "#5b6472", "primary": "#2f5fe0", "primary_hi": "#2450c8",
        "entry": "#ffffff", "border": "#d5dae2", "sel": "#2f5fe0",
        "sel_fg": "#ffffff", "trough": "#e2e7ef", "ok": "#1f7a3d",
        "err": "#c0392b", "link": "#2450c8", "code_bg": "#eef1f6",
    },
    "dark": {
        "bg": "#0f1115", "surface": "#1a1e24", "text": "#f1f3f7",
        "muted": "#9aa4b2", "primary": "#5b86f7", "primary_hi": "#7098ff",
        "entry": "#1a1e24", "border": "#2a2f38", "sel": "#5b86f7",
        "sel_fg": "#0f1115", "trough": "#2a2f38", "ok": "#5bd68a",
        "err": "#ff6b5e", "link": "#7098ff", "code_bg": "#12161c",
    },
}


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


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter imported only inside build_app/main)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to a live tkinter import.

    Kept inside a function so this module imports cleanly without a display.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from html.parser import HTMLParser

    from . import guiconfig, links, render, search, vault
    from .errors import NoteNestError

    FONT = "Segoe UI"
    MONO = "Consolas"

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
    class App(tk.Tk):
        AUTOSAVE_MS = 700

        def __init__(self, notebook=None):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("1180x720")
            self.minsize(920, 560)

            self.theme = guiconfig.get_theme()
            self.root_dir = os.path.abspath(
                notebook or guiconfig.get_notebook()
                or guiconfig.default_notebook_dir())
            self._tracked = []          # (tk_widget, role) for manual re-theming
            self._img_refs = []
            self._busy = False
            self._current = None        # current note name
            self._dirty = False
            self._autosave_job = None
            self._preview_job = None
            self._link_seq = 0

            try:
                vault.ensure_notebook(self.root_dir)
            except NoteNestError:
                pass
            guiconfig.set_notebook(self.root_dir)

            self._set_icon()
            self._build_menu()
            self._build_layout()
            self._apply_theme()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._startup)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("note-nest.ico")
                if ico:
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

        # ---- theming
        def track(self, widget, role):
            self._tracked.append((widget, role))

        def _pal(self):
            return PALETTES[self.theme]

        def _apply_theme(self):
            p = self._pal()
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            self.configure(bg=p["bg"])
            style.configure(".", background=p["bg"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            font=(FONT, 10))
            style.configure("TFrame", background=p["bg"])
            style.configure("Sidebar.TFrame", background=p["surface"])
            style.configure("TLabel", background=p["bg"], foreground=p["text"])
            style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
            style.configure("Header.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 14, "bold"))
            style.configure("Brand.TLabel", background=p["surface"],
                            foreground=p["text"], font=(FONT, 12, "bold"))
            style.configure("Status.TLabel", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TButton", background=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], focuscolor=p["surface"],
                            padding=(10, 5))
            style.map("TButton",
                      background=[("active", p["trough"]), ("disabled", p["bg"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Accent.TButton", background=p["primary"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Accent.TButton",
                      background=[("active", p["primary_hi"]),
                                  ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Toggle.TButton", background=p["surface"],
                            foreground=p["text"], padding=(8, 4))
            for name in ("TEntry", "TSpinbox"):
                style.configure(name, fieldbackground=p["entry"], foreground=p["text"],
                                insertcolor=p["text"], bordercolor=p["border"])
            style.configure("TCombobox", fieldbackground=p["entry"],
                            foreground=p["text"], background=p["surface"],
                            arrowcolor=p["text"])
            style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])])
            style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
            style.configure("TNotebook.Tab", background=p["surface"],
                            foreground=p["muted"], padding=(10, 5))
            style.map("TNotebook.Tab",
                      background=[("selected", p["bg"])],
                      foreground=[("selected", p["text"])])
            style.configure("TScrollbar", background=p["surface"],
                            troughcolor=p["bg"], bordercolor=p["border"],
                            arrowcolor=p["text"])
            style.configure("TSeparator", background=p["border"])
            style.configure("TPanedwindow", background=p["bg"])

            # manually re-colour raw tk widgets (Listbox / Text)
            for widget, role in list(self._tracked):
                try:
                    if role == "listbox":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                    elif role == "editor":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0, font=(MONO, 11))
                    elif role == "preview":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0, font=(FONT, 11))
                        self._configure_preview_tags(widget)
                except Exception:
                    pass

        def _configure_preview_tags(self, w):
            p = self._pal()
            w.tag_configure("h1", font=(FONT, 18, "bold"), spacing1=6, spacing3=4)
            w.tag_configure("h2", font=(FONT, 15, "bold"), spacing1=5, spacing3=3)
            w.tag_configure("h3", font=(FONT, 13, "bold"), spacing1=4, spacing3=2)
            w.tag_configure("bold", font=(FONT, 11, "bold"))
            w.tag_configure("italic", font=(FONT, 11, "italic"))
            w.tag_configure("code", font=(MONO, 10), background=p["code_bg"])
            w.tag_configure("pre", font=(MONO, 10), background=p["code_bg"],
                            lmargin1=12, lmargin2=12)
            w.tag_configure("quote", foreground=p["muted"], lmargin1=12, lmargin2=12)
            w.tag_configure("link", foreground=p["link"], underline=1)

        def toggle_theme(self):
            self.theme = "dark" if self.theme == "light" else "light"
            guiconfig.set_theme(self.theme)
            self._apply_theme()
            self._theme_btn.configure(
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._refresh_preview()

        # ---- menu
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Open notebook…", command=self._open_notebook)
            filem.add_command(label="New note", accelerator="Ctrl+N",
                              command=self._new_note)
            filem.add_command(label="Save", accelerator="Ctrl+S",
                              command=self._save_current)
            filem.add_separator()
            filem.add_command(label="Reindex search", command=self._reindex)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(label="Toggle dark mode", command=self.toggle_theme)
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=self._about)
            helpm.add_command(label="Open project page (quickopen.ai)",
                              command=lambda: open_with_default_app(PROJECT_URL))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)
            self.bind_all("<Control-n>", lambda e: self._new_note())
            self.bind_all("<Control-s>", lambda e: (self._save_current(), "break"))

        # ---- layout
        def _build_layout(self):
            top = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 8))
            top.pack(fill="x", side="top")
            ttk.Label(top, text="NoteNest", style="Brand.TLabel").pack(side="left")
            ttk.Label(top, style="Status.TLabel",
                      text="  offline · open source · by QuickOpen").pack(side="left")
            self._theme_btn = ttk.Button(
                top, style="Toggle.TButton", command=self.toggle_theme,
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._theme_btn.pack(side="right")
            self._nb_btn = ttk.Button(top, style="Toggle.TButton",
                                      command=self._open_notebook, text="📁 Notebook")
            self._nb_btn.pack(side="right", padx=(0, 6))

            panes = ttk.Panedwindow(self, orient="horizontal")
            panes.pack(fill="both", expand=True)

            # ---- left: search + tag filter + note list
            left = ttk.Frame(panes, style="Sidebar.TFrame", padding=8)
            panes.add(left, weight=0)
            ttk.Label(left, text="Search", style="Muted.TLabel").pack(anchor="w")
            self.search_var = tk.StringVar()
            se = ttk.Entry(left, textvariable=self.search_var)
            se.pack(fill="x", pady=(0, 6))
            self.search_var.trace_add("write", lambda *_: self._schedule_filter())

            row = ttk.Frame(left, style="Sidebar.TFrame")
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text="Tag", style="Muted.TLabel").pack(side="left")
            self.tag_var = tk.StringVar(value="(all)")
            self.tag_combo = ttk.Combobox(row, textvariable=self.tag_var,
                                          state="readonly", width=16)
            self.tag_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self.tag_combo.bind("<<ComboboxSelected>>",
                                lambda e: self._refresh_notes())

            self.note_list = tk.Listbox(left, activestyle="none",
                                        selectmode="browse", exportselection=False,
                                        width=26)
            self.note_list.pack(fill="both", expand=True, pady=(0, 6))
            self.note_list.bind("<<ListboxSelect>>", self._on_note_select)
            self.track(self.note_list, "listbox")

            btns = ttk.Frame(left, style="Sidebar.TFrame")
            btns.pack(fill="x")
            ttk.Button(btns, text="New", style="Accent.TButton",
                       command=self._new_note).pack(side="left")
            ttk.Button(btns, text="Delete",
                       command=self._delete_note).pack(side="left", padx=6)

            # ---- centre: editor
            centre = ttk.Frame(panes, style="TFrame", padding=(8, 8))
            panes.add(centre, weight=3)
            head = ttk.Frame(centre, style="TFrame")
            head.pack(fill="x")
            self.title_lbl = ttk.Label(head, text="No note selected",
                                       style="Header.TLabel")
            self.title_lbl.pack(side="left")
            ttk.Button(head, text="Save", command=self._save_current).pack(side="right")
            ed_box = ttk.Frame(centre, style="TFrame")
            ed_box.pack(fill="both", expand=True, pady=(6, 0))
            self.editor = tk.Text(ed_box, wrap="word", undo=True)
            esb = ttk.Scrollbar(ed_box, orient="vertical", command=self.editor.yview)
            self.editor.configure(yscrollcommand=esb.set)
            esb.pack(side="right", fill="y")
            self.editor.pack(side="left", fill="both", expand=True)
            self.track(self.editor, "editor")
            self.editor.bind("<<Modified>>", self._on_editor_modified)

            # ---- right: preview + backlinks/links tabs
            right = ttk.Frame(panes, style="TFrame", padding=(8, 8))
            panes.add(right, weight=2)
            ttk.Label(right, text="Preview", style="Muted.TLabel").pack(anchor="w")
            pv_box = ttk.Frame(right, style="TFrame")
            pv_box.pack(fill="both", expand=True, pady=(2, 6))
            self.preview = tk.Text(pv_box, wrap="word", state="disabled",
                                   cursor="arrow")
            psb = ttk.Scrollbar(pv_box, orient="vertical", command=self.preview.yview)
            self.preview.configure(yscrollcommand=psb.set)
            psb.pack(side="right", fill="y")
            self.preview.pack(side="left", fill="both", expand=True)
            self.track(self.preview, "preview")

            tabs = ttk.Notebook(right, height=180)
            tabs.pack(fill="x")
            bl_frame = ttk.Frame(tabs, style="TFrame")
            self.backlinks_list = tk.Listbox(bl_frame, activestyle="none",
                                             exportselection=False, height=7)
            self.backlinks_list.pack(fill="both", expand=True)
            self.backlinks_list.bind("<Double-Button-1>",
                                     lambda e: self._open_from_list(self.backlinks_list))
            self.track(self.backlinks_list, "listbox")
            tabs.add(bl_frame, text="Backlinks")

            out_frame = ttk.Frame(tabs, style="TFrame")
            self.links_list = tk.Listbox(out_frame, activestyle="none",
                                         exportselection=False, height=7)
            self.links_list.pack(fill="both", expand=True)
            self.links_list.bind("<Double-Button-1>",
                                 lambda e: self._open_from_list(self.links_list))
            self.track(self.links_list, "listbox")
            tabs.add(out_frame, text="Outgoing links")

            # ---- bottom: inline status / error bar
            bar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 6))
            bar.pack(fill="x", side="bottom")
            self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel",
                                        width=18, anchor="w")
            self.status_lbl.pack(side="left")
            self.msg_lbl = ttk.Label(bar, text="", style="Status.TLabel", anchor="w",
                                     wraplength=820, justify="left")
            self.msg_lbl.pack(side="left", fill="x", expand=True, padx=8)

        # ---- startup
        def _startup(self):
            self._refresh_tag_filter()
            self._refresh_notes()
            self._reindex(quiet=True)

        # ---- status helpers
        def _set_status(self, text, kind="idle"):
            p = self._pal()
            color = {"working": p["primary"], "ok": p["ok"], "err": p["err"]}.get(
                kind, p["muted"])
            self.status_lbl.configure(text=text, foreground=color)

        def _info(self, message):
            self.msg_lbl.configure(text=message, foreground=self._pal()["muted"])

        def _error(self, message):
            self._set_status("error", kind="err")
            self.msg_lbl.configure(text="✕ " + message, foreground=self._pal()["err"])

        def _ok(self, message):
            self._set_status("done", kind="ok")
            self.msg_lbl.configure(text=message, foreground=self._pal()["ok"])

        # ---- background runner
        def _bg(self, work, on_ok, busy="Working…"):
            if self._busy:
                return
            self._busy = True
            self._set_status(busy, kind="working")

            def run():
                try:
                    res, err = work(), None
                except NoteNestError as ex:
                    res, err = None, str(ex)
                except Exception as ex:
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                if err is not None:
                    self._error(err)
                    return
                try:
                    on_ok(res)
                except Exception as ex:
                    self._error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- notebook / list
        def _open_notebook(self):
            d = filedialog.askdirectory(title="Choose a notebook folder")
            if not d:
                return
            self._save_current()
            self.root_dir = os.path.abspath(d)
            try:
                vault.ensure_notebook(self.root_dir)
            except NoteNestError as ex:
                self._error(str(ex))
                return
            guiconfig.set_notebook(self.root_dir)
            self._current = None
            self.editor.delete("1.0", "end")
            self.title_lbl.configure(text="No note selected")
            self._refresh_tag_filter()
            self._refresh_notes()
            self._reindex(quiet=True)
            self._info(f"Notebook: {self.root_dir}")

        def _refresh_tag_filter(self):
            try:
                tags = sorted(links.all_tags(self.root_dir))
            except NoteNestError:
                tags = []
            self.tag_combo.configure(values=["(all)"] + ["#" + t for t in tags])
            if self.tag_var.get() not in (["(all)"] + ["#" + t for t in tags]):
                self.tag_var.set("(all)")

        def _visible_notes(self):
            query = self.search_var.get().strip()
            tag = self.tag_var.get()
            tag = tag[1:] if tag.startswith("#") else None
            if query:
                try:
                    names = [h.name for h in search.search(self.root_dir, query)]
                except NoteNestError:
                    names = vault.list_notes(self.root_dir)
            else:
                names = vault.list_notes(self.root_dir)
            if tag:
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

        def _refresh_notes(self):
            names = self._visible_notes()
            self.note_list.delete(0, "end")
            for n in names:
                self.note_list.insert("end", n)
            # keep current selection visible
            if self._current in names:
                idx = names.index(self._current)
                self.note_list.selection_clear(0, "end")
                self.note_list.selection_set(idx)
                self.note_list.see(idx)
            self._set_status(f"{len(names)} note(s)")

        def _schedule_filter(self):
            if self._preview_job:
                self.after_cancel(self._preview_job)
            self._preview_job = self.after(250, self._refresh_notes)

        def _on_note_select(self, _e=None):
            sel = self.note_list.curselection()
            if not sel:
                return
            name = self.note_list.get(sel[0])
            if name == self._current:
                return
            self._save_current()
            self._load_note(name)

        def _load_note(self, name):
            try:
                text = vault.read_note(self.root_dir, name)
            except NoteNestError as ex:
                self._error(str(ex))
                return
            self._current = name
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", text)
            self.editor.edit_reset()
            self.editor.edit_modified(False)
            self._dirty = False
            self.title_lbl.configure(text=name)
            self._refresh_preview()
            self._refresh_side_panels()

        # ---- editing / autosave
        def _on_editor_modified(self, _e=None):
            if not self.editor.edit_modified():
                return
            self.editor.edit_modified(False)
            if self._current is None:
                return
            self._dirty = True
            self._set_status("editing…")
            if self._autosave_job:
                self.after_cancel(self._autosave_job)
            self._autosave_job = self.after(self.AUTOSAVE_MS, self._autosave)
            # live preview (cheap; render is fast)
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
                self._error(str(ex))
                return
            self._dirty = False
            self._ok(f"Saved {self._current}")
            # incremental index update + side panels off the UI thread
            name = self._current
            self._bg(lambda: search.update_note(self.root_dir, name),
                     lambda _r: None, busy="Indexing…")
            self._refresh_tag_filter()
            self._refresh_side_panels()

        def _new_note(self):
            title = simpledialog.askstring("New note", "Note title:", parent=self)
            if not title:
                return
            title = title.strip()
            if not title:
                return
            if vault.note_exists(self.root_dir, title):
                self._error(f"A note named {title!r} already exists.")
                self._select_note(title)
                return
            try:
                vault.write_note(self.root_dir, title, f"# {title}\n\n")
            except NoteNestError as ex:
                self._error(str(ex))
                return
            self._bg(lambda: search.update_note(self.root_dir, title),
                     lambda _r: None, busy="Indexing…")
            self.search_var.set("")
            self.tag_var.set("(all)")
            self._refresh_notes()
            self._select_note(title)
            self._ok(f"Created {title}")

        def _delete_note(self):
            if self._current is None:
                self._info("Select a note to delete.")
                return
            name = self._current
            if not messagebox.askyesno("Delete note",
                                       f"Delete '{name}'? This cannot be undone."):
                return
            try:
                vault.delete_note(self.root_dir, name)
                search.remove_note(self.root_dir, name)
            except NoteNestError as ex:
                self._error(str(ex))
                return
            self._current = None
            self._dirty = False
            self.editor.delete("1.0", "end")
            self.title_lbl.configure(text="No note selected")
            self._clear_preview()
            self._refresh_tag_filter()
            self._refresh_notes()
            self._ok(f"Deleted {name}")

        def _select_note(self, name):
            names = list(self.note_list.get(0, "end"))
            if name in names:
                idx = names.index(name)
                self.note_list.selection_clear(0, "end")
                self.note_list.selection_set(idx)
                self.note_list.see(idx)
                self._load_note(name)

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
            self.preview.tag_configure(tag, foreground=self._pal()["link"],
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
                self._error(str(ex))
                return
            if not name:
                self._error(f"Could not resolve link: {target}")
                return
            self._bg(lambda: search.update_note(self.root_dir, name),
                     lambda _r: None, busy="Indexing…")
            self.search_var.set("")
            self._refresh_tag_filter()
            self._refresh_notes()
            self._select_note(name)

        def _refresh_side_panels(self):
            self.backlinks_list.delete(0, "end")
            self.links_list.delete(0, "end")
            if self._current is None:
                return
            try:
                for n in links.backlinks(self.root_dir, self._current):
                    self.backlinks_list.insert("end", n)
                text = vault.read_note(self.root_dir, self._current)
                for target in links.parse_links(text):
                    resolved = links.resolve_link(self.root_dir, target)
                    label = target if resolved else target + "  (new)"
                    self.links_list.insert("end", label)
            except NoteNestError:
                pass

        def _open_from_list(self, listbox):
            sel = listbox.curselection()
            if not sel:
                return
            name = listbox.get(sel[0]).replace("  (new)", "").strip()
            self._follow_link(name)

        # ---- reindex
        def _reindex(self, quiet=False):
            def done(n):
                if not quiet:
                    self._ok(f"Reindexed {n} note(s).")
                else:
                    self._set_status("Ready")
            self._bg(lambda: search.reindex(self.root_dir), done, busy="Indexing…")

        # ---- About
        def _about(self):
            win = tk.Toplevel(self)
            win.title("About NoteNest")
            win.configure(bg=self._pal()["bg"])
            win.resizable(False, False)
            frm = ttk.Frame(win, style="TFrame", padding=18)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="NoteNest", style="Header.TLabel").pack(anchor="w")
            ttk.Label(frm, text=f"Version {APP_VERSION}",
                      style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
            ttk.Label(frm, style="TLabel", justify="left", wraplength=380,
                      text="A fast, fully-offline Markdown knowledge base — "
                           "wiki-links, backlinks, tags and full-text search over "
                           "plain files you own.\n\n"
                           "100% AI-built, open source, published on QuickOpen.\n"
                           "Nothing is ever uploaded anywhere.").pack(anchor="w")
            ttk.Label(frm, style="Muted.TLabel", justify="left", wraplength=380,
                      text="Licensed under Apache-2.0. Built on permissive "
                           "libraries: python-markdown and Whoosh.").pack(
                anchor="w", pady=(8, 4))
            ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e")
            win.transient(self)
            win.grab_set()

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
    With no display (e.g. a server) it prints a friendly note and returns 0
    instead of raising, so headless callers stay clean.
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
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the Windows desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
