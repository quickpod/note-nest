"""notenest -- a local, offline Markdown knowledge base.

A *notebook* is just a folder of ``.md`` files you own.  The package layers a
small, well-tested API over that folder:

    from notenest import vault, links, render, search
    vault.write_note(root, "Ideas", "# Ideas\n\nSee [[Inbox]] #todo")
    links.backlinks(root, "Inbox")
    render.to_html(vault.read_note(root, "Ideas"))
    search.reindex(root); search.search(root, "ideas")

The GUI (:mod:`notenest.gui`) and CLI (:mod:`notenest.__main__`) build on these
modules and never re-implement note logic.  Every operation raises
:class:`NoteNestError` (and only that) on failure.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

from .errors import NoteNestError

__version__ = "1.1.1"

__all__ = ["NoteNestError", "vault", "links", "render", "search", "guiconfig"]
