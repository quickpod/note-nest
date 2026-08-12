r"""Wiki-links and tags -- the connective tissue between notes.

Two lightweight syntaxes are recognised inside a note's Markdown:

* ``[[Target]]`` or ``[[Target|display text]]`` -- a *wiki-link* to another
  note (by its :mod:`~notenest.vault` name).
* ``#tag`` -- an inline *tag*.  A ``#`` is only a tag when it is not followed by
  a space (so Markdown ``# Heading`` lines are never mistaken for tags) and when
  it starts at a word boundary.

From these we can compute *backlinks* (who points at me) and a *graph* of the
whole notebook (nodes + edges).  Link resolution is by note name and is
case-insensitive on the final path component, with an optional create-on-follow.
"""

from __future__ import annotations

import os
import re

from . import vault
from .errors import NoteNestError

_WIKI_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")
# a #tag: preceded by start-of-string or whitespace, then # + tag chars,
# where the char right after # is NOT whitespace (rules out "# Heading").
_TAG_RE = re.compile(r"(?:(?<=\s)|^)#([A-Za-z0-9][A-Za-z0-9_\-/]*)")


def _dedupe(seq):
    seen = set()
    out = []
    for item in seq:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def parse_links(text):
    """Return the ordered, de-duplicated list of wiki-link targets in *text*."""
    if not text:
        return []
    targets = [m.group(1).strip() for m in _WIKI_RE.finditer(text)]
    return _dedupe([t for t in targets if t])


def parse_tags(text):
    """Return the ordered, de-duplicated list of tags (without the ``#``)."""
    if not text:
        return []
    return _dedupe([m.group(1) for m in _TAG_RE.finditer(text)])


def resolve_link(root, target, create=False):
    """Resolve a wiki-link *target* to a note name, or ``None`` if unresolved.

    Matching prefers an exact name/path match, then a case-insensitive match on
    the final path component (the title).  With ``create=True`` a missing target
    is created as an empty note and its name returned.
    """
    target = (target or "").strip()
    if not target:
        return None

    names = vault.list_notes(root)
    lut = {n.lower(): n for n in names}

    # exact name (with or without folder) match
    if target.lower() in lut:
        return lut[target.lower()]

    # match on the last path component (the title only)
    title = target.split("/")[-1].lower()
    for n in names:
        if n.split("/")[-1].lower() == title:
            return n

    if create:
        vault.write_note(root, target, "")
        return vault.note_name(root, vault.note_path(root, target))
    return None


def backlinks(root, note):
    """Return the sorted list of note names whose text links to *note*.

    *note* may be a name or a path; a note that links to itself is not counted.
    """
    target = vault.note_name(root, vault.note_path(root, note))
    hits = []
    for name in vault.list_notes(root):
        if name == target:
            continue
        try:
            text = vault.read_note(root, name)
        except NoteNestError:
            continue
        for link in parse_links(text):
            if resolve_link(root, link) == target:
                hits.append(name)
                break
    return sorted(hits)


def graph(root):
    """Return ``{"nodes": [...], "edges": [...]}`` for the whole notebook.

    Nodes are note names (sorted).  Edges are ``(source, target)`` pairs for
    every wiki-link that resolves to an existing note (de-duplicated, sorted).
    """
    names = vault.list_notes(root)
    edges = set()
    for name in names:
        try:
            text = vault.read_note(root, name)
        except NoteNestError:
            continue
        for link in parse_links(text):
            dst = resolve_link(root, link)
            if dst is not None and dst != name:
                edges.add((name, dst))
    return {"nodes": sorted(names), "edges": sorted(edges)}


def all_tags(root):
    """Return ``{tag: count}`` across every note in the notebook."""
    counts = {}
    for name in vault.list_notes(root):
        try:
            text = vault.read_note(root, name)
        except NoteNestError:
            continue
        for tag in parse_tags(text):
            counts[tag] = counts.get(tag, 0) + 1
    return counts
