r"""The notebook (vault) -- plain folders of ``.md`` files the user owns.

A *notebook* is just a directory; every ``*.md`` file inside it (recursively) is
a *note*.  A note is addressed by its **name**: the path relative to the
notebook root, using forward slashes, without the ``.md`` extension.  For a flat
notebook that is simply the file's title, e.g. ``Ideas``; in a sub-folder it is
``projects/Ideas``.

Functions accept a *name* OR an explicit path (absolute, or relative to the
root) and normalise it, so callers never have to build ``.md`` paths by hand.
Everything raises :class:`NoteNestError` on failure and nothing here imports the
GUI, Whoosh or Markdown -- this is the storage layer.
"""

from __future__ import annotations

import os

from .errors import NoteNestError

NOTE_EXT = ".md"


# ---------------------------------------------------------------------------
# name / path normalisation
# ---------------------------------------------------------------------------
def note_name(root, path):
    """Return the notebook-relative *name* for a note file *path*."""
    root = os.path.abspath(root)
    rel = os.path.relpath(os.path.abspath(path), root)
    if rel.lower().endswith(NOTE_EXT):
        rel = rel[: -len(NOTE_EXT)]
    return rel.replace(os.sep, "/")


def note_path(root, name):
    """Resolve a note *name* (or path) to an absolute ``.md`` file path.

    Rejects names that would escape the notebook root (path traversal).
    """
    if name is None:
        raise NoteNestError("No note name was given.")
    raw = str(name).strip()
    if not raw:
        raise NoteNestError("No note name was given.")

    if os.path.isabs(raw):
        target = raw
    else:
        rel = raw.replace("\\", "/")
        if not rel.lower().endswith(NOTE_EXT):
            rel += NOTE_EXT
        target = os.path.join(root, *rel.split("/"))

    target = os.path.abspath(target)
    if not target.lower().endswith(NOTE_EXT):
        target += NOTE_EXT

    root_abs = os.path.abspath(root)
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise NoteNestError(f"Note path escapes the notebook: {name!r}")
    return target


def ensure_notebook(root):
    """Create the notebook directory if needed; return its absolute path."""
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        raise NoteNestError(f"Could not create notebook {root!r}: {exc}") from exc
    if not os.path.isdir(root):
        raise NoteNestError(f"Notebook is not a directory: {root!r}")
    return os.path.abspath(root)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_notes(root):
    """Return a sorted list of note names found under *root* (recursively)."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return []
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        # skip hidden dirs (e.g. a stray .git or the index dir if nested)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.lower().endswith(NOTE_EXT) and not fn.startswith("."):
                names.append(note_name(root, os.path.join(dirpath, fn)))
    return sorted(names)


def note_exists(root, name):
    try:
        return os.path.isfile(note_path(root, name))
    except NoteNestError:
        return False


def read_note(root, name):
    """Return the full Markdown text of a note."""
    path = note_path(root, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise NoteNestError(f"Note not found: {name!r}")
    except OSError as exc:
        raise NoteNestError(f"Could not read note {name!r}: {exc}") from exc


def write_note(root, name, text):
    """Create or overwrite a note; returns its absolute path.

    Parent sub-folders are created as needed.  *text* is written verbatim as
    UTF-8 (the file is the user's; we do not reformat it).
    """
    path = note_path(root, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text is not None else "")
        os.replace(tmp, path)
    except OSError as exc:
        raise NoteNestError(f"Could not write note {name!r}: {exc}") from exc
    return path


def delete_note(root, name):
    """Delete a note file.  Raises if it does not exist."""
    path = note_path(root, name)
    try:
        os.remove(path)
    except FileNotFoundError:
        raise NoteNestError(f"Note not found: {name!r}")
    except OSError as exc:
        raise NoteNestError(f"Could not delete note {name!r}: {exc}") from exc
    return True


def rename(root, old, new):
    """Rename/move a note from *old* to *new*; returns the new absolute path."""
    src = note_path(root, old)
    dst = note_path(root, new)
    if not os.path.isfile(src):
        raise NoteNestError(f"Note not found: {old!r}")
    if os.path.exists(dst):
        raise NoteNestError(f"A note named {new!r} already exists.")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
    except OSError as exc:
        raise NoteNestError(f"Could not rename {old!r} -> {new!r}: {exc}") from exc
    return dst
