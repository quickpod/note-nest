#!/usr/bin/env python3
r"""NoteNest -- an Aura (QuickOpen design system) GUI on top of the ``notenest`` API.

A single Aura window with two sidebar sections:

  * **Notes** -- the writing workspace: a three-pane splitter with a search +
    tag-filter + note list on the left, the Markdown **editor** (autosaves as
    you type) in the centre, and a live rendered **preview** plus a tabbed
    backlinks / outgoing-links panel on the right.  Wiki-links in the preview
    are clickable and navigate (creating the target note if it does not exist).
  * **About** -- app / licence info.

Design goals baked in here (mirrors the QuickOpen house style):
  * built on the vendored ``notenest/aura.py`` design system, which layers the
    quickopen.ai look (deep space + light) over CustomTkinter.  Runtime deps:
    ``customtkinter`` (+ ``darkdetect``) — declared in requirements.txt; the
    PyInstaller build adds ``--collect-all customtkinter``.
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

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (packaging, headless CI) never fails.

APP_NAME = "NoteNest"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "NoteNest — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#17914b"      # publish/specs/note-nest.json "accent": [23, 145, 75]


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
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports.

    Kept inside a function so this module imports cleanly without a display
    (and without customtkinter installed).
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
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
                size=(1180, 720), min_size=(920, 560))

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

            try:
                vault.ensure_notebook(self.root_dir)
            except NoteNestError:
                pass
            guiconfig.set_notebook(self.root_dir)

            self._set_icon()
            self._build_menu()
            self.add_section("notes", "Notes", "✎", self._build_notes)
            self.add_section("about", "About", "ℹ", self._build_about)
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

        # ---- menu (native menus stay; theme also lives in the sidebar toggle)
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
            self.bind_all("<Control-n>", lambda e: self._new_note())
            self.bind_all("<Control-s>", lambda e: (self._save_current(), "break"))

        # =================================================================
        # Notes section — the three-pane workspace
        # =================================================================
        def _build_notes(self, frame):
            panes = ttk.Panedwindow(frame, orient="horizontal")
            panes.pack(fill="both", expand=True)

            # ---- left: notebook controls + search + tag filter + note list
            left = ttk.Frame(panes)
            panes.add(left, weight=0)

            nb_row = ctk.CTkFrame(left, fg_color="transparent")
            nb_row.pack(fill="x", pady=(0, 8))
            aura.SectionLabel(nb_row, "Notebook").pack(side="left")
            aura.AuraButton(nb_row, "Open…", kind="ghost", height=28,
                            command=self._open_notebook).pack(side="right")
            self._nb_caption = aura.Caption(left, self._notebook_label())
            self._nb_caption.pack(anchor="w", pady=(0, 10))

            aura.SectionLabel(left, "Search").pack(anchor="w")
            # no textvariable: CTkEntry placeholders only work without one
            self.search_entry = aura.AuraEntry(
                left, placeholder="Search names and contents…")
            self.search_entry.pack(fill="x", pady=(2, 8))
            self.search_entry.bind("<KeyRelease>",
                                   lambda _e: self._schedule_filter())

            tag_row = ctk.CTkFrame(left, fg_color="transparent")
            tag_row.pack(fill="x", pady=(0, 8))
            aura.SectionLabel(tag_row, "Tag").pack(side="left", padx=(0, 8))
            self.tag_var = tk.StringVar(value="(all)")
            self.tag_combo = aura.AuraCombo(
                tag_row, variable=self.tag_var, values=["(all)"],
                state="readonly", command=lambda _v: self._refresh_notes())
            self.tag_combo.pack(side="left", fill="x", expand=True)

            list_wrap = ctk.CTkFrame(left, fg_color=pair("surface"),
                                     corner_radius=10,
                                     border_width=1, border_color=pair("border"))
            list_wrap.pack(fill="both", expand=True, pady=(0, 8))
            self.note_list = tk.Listbox(list_wrap, activestyle="none",
                                        selectmode="browse",
                                        exportselection=False, width=26,
                                        font=(UI_FAMILY, 10))
            nsb = aura.AuraScrollbar(list_wrap, command=self.note_list.yview)
            self.note_list.configure(yscrollcommand=nsb.set)
            nsb.pack(side="right", fill="y", padx=(0, 4), pady=4)
            self.note_list.pack(side="left", fill="both", expand=True,
                                padx=(6, 0), pady=6)
            self.note_list.bind("<<ListboxSelect>>", self._on_note_select)
            aura.track(self.note_list, "listbox")

            btns = ctk.CTkFrame(left, fg_color="transparent")
            btns.pack(fill="x")
            aura.AuraButton(btns, "New note", kind="primary",
                            command=self._new_note).pack(side="left")
            aura.AuraButton(btns, "Delete", kind="danger",
                            command=self._delete_note).pack(side="left",
                                                            padx=(8, 0))

            # ---- centre: editor
            centre = ttk.Frame(panes)
            panes.add(centre, weight=3)
            head = ctk.CTkFrame(centre, fg_color="transparent")
            head.pack(fill="x", pady=(0, 8))
            self.title_lbl = aura.Heading(head, "No note selected")
            self.title_lbl.pack(side="left")
            aura.AuraButton(head, "Save", kind="secondary", height=30,
                            command=self._save_current).pack(side="right")

            ed_wrap = ctk.CTkFrame(centre, fg_color=pair("field"),
                                   corner_radius=10,
                                   border_width=1, border_color=pair("border"))
            ed_wrap.pack(fill="both", expand=True)
            self.editor = tk.Text(ed_wrap, wrap="word", undo=True,
                                  relief="flat", padx=10, pady=8,
                                  font=(MONO_FAMILY, 11))
            esb = aura.AuraScrollbar(ed_wrap, command=self.editor.yview)
            self.editor.configure(yscrollcommand=esb.set)
            esb.pack(side="right", fill="y", padx=(0, 4), pady=4)
            self.editor.pack(side="left", fill="both", expand=True,
                             padx=(4, 0), pady=4)
            aura.track(self.editor, "text")
            self.editor.bind("<<Modified>>", self._on_editor_modified)

            # ---- right: preview + backlinks/outgoing tabs
            right = ttk.Frame(panes)
            panes.add(right, weight=2)
            aura.SectionLabel(right, "Preview").pack(anchor="w", pady=(0, 4))
            pv_wrap = ctk.CTkFrame(right, fg_color=pair("field"),
                                   corner_radius=10,
                                   border_width=1, border_color=pair("border"))
            pv_wrap.pack(fill="both", expand=True, pady=(0, 8))
            self.preview = tk.Text(pv_wrap, wrap="word", state="disabled",
                                   cursor="arrow", relief="flat",
                                   padx=10, pady=8, font=(UI_FAMILY, 11))
            psb = aura.AuraScrollbar(pv_wrap, command=self.preview.yview)
            self.preview.configure(yscrollcommand=psb.set)
            psb.pack(side="right", fill="y", padx=(0, 4), pady=4)
            self.preview.pack(side="left", fill="both", expand=True,
                              padx=(4, 0), pady=4)
            aura.track(self.preview, "text")
            self._configure_preview_tags(self.preview)

            tabs = ttk.Notebook(right, height=180)
            tabs.pack(fill="x")
            bl_frame = ttk.Frame(tabs)
            self.backlinks_list = tk.Listbox(bl_frame, activestyle="none",
                                             exportselection=False, height=7,
                                             font=(UI_FAMILY, 10))
            self.backlinks_list.pack(fill="both", expand=True)
            self.backlinks_list.bind(
                "<Double-Button-1>",
                lambda e: self._open_from_list(self.backlinks_list))
            aura.track(self.backlinks_list, "listbox")
            tabs.add(bl_frame, text=aura.spaced("Backlinks"))

            out_frame = ttk.Frame(tabs)
            self.links_list = tk.Listbox(out_frame, activestyle="none",
                                         exportselection=False, height=7,
                                         font=(UI_FAMILY, 10))
            self.links_list.pack(fill="both", expand=True)
            self.links_list.bind(
                "<Double-Button-1>",
                lambda e: self._open_from_list(self.links_list))
            aura.track(self.links_list, "listbox")
            tabs.add(out_frame, text=aura.spaced("Outgoing links"))

        def _notebook_label(self):
            try:
                return "▤  " + os.path.basename(os.path.normpath(self.root_dir))
            except Exception:
                return self.root_dir

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
        def set_theme(self, theme):
            super().set_theme(theme)
            try:
                self._configure_preview_tags(self.preview)
                self._refresh_preview()
            except Exception:
                pass

        # ---- startup
        def _startup(self):
            self._refresh_tag_filter()
            self._refresh_notes()
            self._reindex(quiet=True)
            self.after(60, lambda: self.search_entry.focus_set())

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
                self.after(0, lambda: finish(res, err))

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
                self.set_error(str(ex))
                return
            guiconfig.set_notebook(self.root_dir)
            self._current = None
            self.editor.delete("1.0", "end")
            self.title_lbl.configure(text="No note selected")
            if hasattr(self, "_nb_caption"):
                self._nb_caption.configure(text=self._notebook_label())
            self._refresh_tag_filter()
            self._refresh_notes()
            self._reindex(quiet=True)
            self.set_success(f"Notebook: {self.root_dir}")

        def _refresh_tag_filter(self):
            try:
                tags = sorted(links.all_tags(self.root_dir))
            except NoteNestError:
                tags = []
            values = ["(all)"] + ["#" + t for t in tags]
            self.tag_combo.configure(values=values)
            if self.tag_var.get() not in values:
                self.tag_var.set("(all)")

        def _visible_notes(self):
            query = self.search_entry.get().strip()
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
            self.set_status(f"{len(names)} note(s)")

        def _schedule_filter(self):
            if self._filter_job:
                self.after_cancel(self._filter_job)
            self._filter_job = self.after(250, self._refresh_notes)

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
                self.set_error(str(ex))
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
            self.set_status("editing…")
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
                self.set_error(str(ex))
                return
            self._dirty = False
            self.set_success(f"Saved {self._current}")
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
                self.set_error(f"A note named {title!r} already exists.")
                self._select_note(title)
                return
            try:
                vault.write_note(self.root_dir, title, f"# {title}\n\n")
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self._bg(lambda: search.update_note(self.root_dir, title),
                     lambda _r: None, busy="Indexing…")
            self._clear_search()
            self.tag_var.set("(all)")
            self._refresh_notes()
            self._select_note(title)
            self.set_success(f"Created {title}")

        def _delete_note(self):
            if self._current is None:
                self.set_status("Select a note to delete.")
                return
            name = self._current
            if not messagebox.askyesno("Delete note",
                                       f"Delete '{name}'? This cannot be undone."):
                return
            try:
                vault.delete_note(self.root_dir, name)
                search.remove_note(self.root_dir, name)
            except NoteNestError as ex:
                self.set_error(str(ex))
                return
            self._current = None
            self._dirty = False
            self.editor.delete("1.0", "end")
            self.title_lbl.configure(text="No note selected")
            self._clear_preview()
            self._refresh_tag_filter()
            self._refresh_notes()
            self.set_success(f"Deleted {name}")

        def _clear_search(self):
            try:
                self.search_entry.delete(0, "end")
            except Exception:
                pass

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
                     lambda _r: None, busy="Indexing…")
            self._clear_search()
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
                    self.set_success(f"Reindexed {n} note(s).")
                else:
                    self.set_status("Ready")
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
                     "wiki-links, backlinks, tags and full-text search over "
                     "plain files you own.\n\n"
                     "100% AI-built, open source, published on QuickOpen. "
                     "Nothing is ever uploaded anywhere.").pack(anchor="w")
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
              f"GUI here ({exc}). This app is intended for the Windows desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
