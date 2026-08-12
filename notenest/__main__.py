"""Command-line interface: ``python -m notenest <command> ...``.

Commands: ``new``, ``list``, ``show``, ``search``, ``backlinks``, ``tags``,
``reindex``.  A ``--notebook DIR`` option (global) picks the notebook folder;
without it the last-used notebook is reused, falling back to a default folder
under the per-user config directory.  Every command exits cleanly with a
one-line ``error: ...`` message (never a traceback) when a
:class:`NoteNestError` is raised.
"""

from __future__ import annotations

import argparse
import sys

from . import guiconfig, links, render, search, vault
from .errors import NoteNestError


# ---------------------------------------------------------------------------
# notebook resolution
# ---------------------------------------------------------------------------
def _resolve_notebook(arg):
    root = arg or guiconfig.get_notebook() or guiconfig.default_notebook_dir()
    root = vault.ensure_notebook(root)
    if arg:  # remember an explicitly chosen notebook for next time
        guiconfig.set_notebook(root)
    return root


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------
def cmd_new(a):
    root = _resolve_notebook(a.notebook)
    if vault.note_exists(root, a.title) and not a.force:
        raise NoteNestError(
            f"Note {a.title!r} already exists (use --force to overwrite).")
    body = a.text if a.text is not None else f"# {a.title}\n\n"
    path = vault.write_note(root, a.title, body)
    try:
        search.update_note(root, a.title)
    except NoteNestError:
        pass  # a stale index must not fail note creation
    print(path)


def cmd_list(a):
    root = _resolve_notebook(a.notebook)
    names = vault.list_notes(root)
    if not names:
        print("(no notes yet)")
        return
    for name in names:
        print(name)


def cmd_show(a):
    root = _resolve_notebook(a.notebook)
    text = vault.read_note(root, a.note)
    if a.html:
        print(render.to_html(text))
    else:
        print(render.to_text(text))


def cmd_search(a):
    root = _resolve_notebook(a.notebook)
    hits = search.search(root, a.query, limit=a.limit)
    if not hits:
        print("(no matches)")
        return
    for hit in hits:
        tags = ("  #" + " #".join(hit.tags)) if hit.tags else ""
        print(f"{hit.name}{tags}")
        if hit.snippet:
            print(f"    {hit.snippet}")


def cmd_backlinks(a):
    root = _resolve_notebook(a.notebook)
    # validate the target exists so a typo is reported clearly
    vault.read_note(root, a.note)
    back = links.backlinks(root, a.note)
    if not back:
        print("(no backlinks)")
        return
    for name in back:
        print(name)


def cmd_tags(a):
    root = _resolve_notebook(a.notebook)
    counts = links.all_tags(root)
    if not counts:
        print("(no tags)")
        return
    for tag in sorted(counts):
        print(f"{tag}\t{counts[tag]}")


def cmd_reindex(a):
    root = _resolve_notebook(a.notebook)
    n = search.reindex(root)
    print(f"Indexed {n} note(s).")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="notenest",
        description="NoteNest — a local Markdown knowledge base.")
    p.add_argument("--notebook", metavar="DIR",
                   help="notebook folder (defaults to the last-used one)")
    sub = p.add_subparsers(dest="command")

    def add(name, help_text, func):
        s = sub.add_parser(name, help=help_text)
        s.set_defaults(func=func)
        return s

    s = add("new", "Create a new note", cmd_new)
    s.add_argument("title")
    s.add_argument("--text", help="initial body (default: a '# Title' heading)")
    s.add_argument("--force", action="store_true", help="overwrite if it exists")

    add("list", "List all notes", cmd_list)

    s = add("show", "Render a note to text (or HTML)", cmd_show)
    s.add_argument("note")
    s.add_argument("--html", action="store_true", help="output HTML instead of text")

    s = add("search", "Full-text search the notebook", cmd_search)
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)

    s = add("backlinks", "Show notes that link to a note", cmd_backlinks)
    s.add_argument("note")

    add("tags", "List all tags with counts", cmd_tags)

    add("reindex", "Rebuild the search index", cmd_reindex)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        args.func(args)
    except NoteNestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
