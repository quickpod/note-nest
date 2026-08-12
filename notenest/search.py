r"""Full-text search over a notebook, backed by Whoosh.

The index lives under the per-user config dir (``guiconfig.index_root()``), one
sub-directory per notebook keyed by a hash of the notebook's absolute path, so
several notebooks never collide.  Each note is one document with a stored
``content`` field (title + plain body + tags) used both for matching and for
building the result snippet.

Public API:
  * :func:`reindex(root)` -- rebuild the whole index; returns the note count.
  * :func:`search(root, query, limit=)` -- run a query, return ranked hits with
    a snippet.
  * :func:`update_note(root, name)` / :func:`remove_note(root, name)` --
    incremental single-document updates (called by the GUI/CLI on save/delete).

All failures raise :class:`NoteNestError`.
"""

from __future__ import annotations

import hashlib
import os

from . import guiconfig, links, render, vault
from .errors import NoteNestError


class SearchHit:
    """One search result (attribute access + dict-ish for convenience)."""

    __slots__ = ("name", "title", "tags", "snippet", "score")

    def __init__(self, name, title, tags, snippet, score):
        self.name = name
        self.title = title
        self.tags = tags
        self.snippet = snippet
        self.score = score

    def __repr__(self):
        return f"SearchHit({self.name!r}, score={self.score:.3f})"


def _schema():
    from whoosh import fields
    return fields.Schema(
        path=fields.ID(unique=True, stored=True),
        title=fields.TEXT(stored=True, field_boost=2.0),
        tags=fields.KEYWORD(stored=True, lowercase=True, commas=True, scorable=True),
        body=fields.TEXT(stored=True),
        content=fields.TEXT(stored=True),
    )


def index_dir(root):
    """Absolute path of the Whoosh index directory for notebook *root*."""
    key = hashlib.sha1(os.path.abspath(root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(guiconfig.index_root(), key)


def _doc_fields(root, name):
    text = vault.read_note(root, name)
    body = render.to_text(text)
    tags = links.parse_tags(text)
    tag_str = ",".join(sorted({t.lower() for t in tags}))
    content = "\n".join([name.split("/")[-1], body, " ".join(tags)])
    return {
        "path": name,
        "title": name.split("/")[-1],
        "tags": tag_str,
        "body": body,
        "content": content,
    }


def _open(root, create=False):
    from whoosh import index
    d = index_dir(root)
    if index.exists_in(d):
        try:
            return index.open_dir(d)
        except Exception as exc:
            if not create:
                raise NoteNestError(f"Could not open search index: {exc}") from exc
    if not create:
        raise NoteNestError("No search index yet — run reindex first.")
    try:
        os.makedirs(d, exist_ok=True)
        return index.create_in(d, _schema())
    except Exception as exc:
        raise NoteNestError(f"Could not create search index: {exc}") from exc


def reindex(root):
    """Rebuild the entire index from scratch; return the number of notes."""
    from whoosh import index
    d = index_dir(root)
    try:
        os.makedirs(d, exist_ok=True)
        ix = index.create_in(d, _schema())   # fresh, empty index (overwrites)
        writer = ix.writer()
        count = 0
        for name in vault.list_notes(root):
            try:
                writer.add_document(**_doc_fields(root, name))
                count += 1
            except NoteNestError:
                continue
        writer.commit()
        return count
    except NoteNestError:
        raise
    except Exception as exc:
        raise NoteNestError(f"Could not reindex: {exc}") from exc


def update_note(root, name):
    """Add or update a single note in the index (creating the index if needed)."""
    name = vault.note_name(root, vault.note_path(root, name))
    try:
        ix = _open(root, create=True)
        writer = ix.writer()
        writer.update_document(**_doc_fields(root, name))
        writer.commit()
    except NoteNestError:
        raise
    except Exception as exc:
        raise NoteNestError(f"Could not update index for {name!r}: {exc}") from exc


def remove_note(root, name):
    """Drop a single note from the index (best-effort; no error if absent)."""
    name = vault.note_name(root, vault.note_path(root, name))
    try:
        ix = _open(root, create=True)
        writer = ix.writer()
        writer.delete_by_term("path", name)
        writer.commit()
    except NoteNestError:
        raise
    except Exception as exc:
        raise NoteNestError(f"Could not remove {name!r} from index: {exc}") from exc


def search(root, query, limit=20, reindex_if_missing=True):
    """Run *query* against the notebook index; return a list of :class:`SearchHit`.

    A bare term matches the note's title, body or tags.  ``tags:foo`` restricts
    to the tag field.  If no index exists yet it is built first (unless
    ``reindex_if_missing`` is false).
    """
    from whoosh import index, highlight
    from whoosh.qparser import MultifieldParser, OrGroup

    q = (query or "").strip()
    if not q:
        return []

    if not index.exists_in(index_dir(root)):
        if reindex_if_missing:
            reindex(root)
        else:
            return []

    ix = _open(root, create=False)
    try:
        parser = MultifieldParser(
            ["content", "title", "body", "tags"], schema=ix.schema, group=OrGroup)
        parsed = parser.parse(q)
        hits = []
        with ix.searcher() as searcher:
            results = searcher.search(parsed, limit=limit)
            results.fragmenter = highlight.ContextFragmenter(maxchars=200, surround=40)
            results.formatter = highlight.UppercaseFormatter()
            for hit in results:
                snippet = ""
                try:
                    snippet = hit.highlights("content") or ""
                except Exception:
                    snippet = ""
                if not snippet:
                    body = hit.get("body") or ""
                    snippet = body[:160]
                tags = [t for t in (hit.get("tags") or "").split(",") if t]
                hits.append(SearchHit(
                    name=hit["path"], title=hit.get("title") or hit["path"],
                    tags=tags, snippet=snippet.replace("\n", " ").strip(),
                    score=float(hit.score or 0.0)))
        return hits
    except NoteNestError:
        raise
    except Exception as exc:
        raise NoteNestError(f"Search failed: {exc}") from exc
