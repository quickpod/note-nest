r"""Tiny JSON-backed config + shared app directories for NoteNest.

Stores the chosen theme ("light"/"dark"), a short list of recent notebook
folders and the last-opened notebook.  On Windows the file lives at
``%LOCALAPPDATA%\NoteNest\config.json``; elsewhere it falls back to
``~/.notenest/config.json``.  Every function is defensive -- a corrupt or
unreadable config must never stop the app (or the CLI) from starting.

This module also owns the two derived directories the rest of the package
relies on: the default notebook folder and the Whoosh index root, both kept
under the same per-user config directory.
"""

from __future__ import annotations

import json
import os

APP_DIRNAME = "NoteNest"
CONFIG_NAME = "config.json"
MAX_RECENT = 10
# "system" follows the OS Aura Dark/Light live (the fresh-install default).
VALID_THEMES = ("system", "light", "dark")
FONT_SIZES = (10, 11, 12, 13, 14, 16)


def config_dir():
    r"""Directory that holds the config file and app data (created on demand).

    ``%LOCALAPPDATA%\NoteNest`` on Windows, ``~/.notenest`` otherwise.  Honours
    ``NOTENEST_HOME`` when set so tests can redirect everything to a tmp dir.
    """
    override = os.environ.get("NOTENEST_HOME")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local and os.name == "nt":
        return os.path.join(local, APP_DIRNAME)
    return os.path.join(os.path.expanduser("~"), "." + APP_DIRNAME.lower())


def config_path():
    return os.path.join(config_dir(), CONFIG_NAME)


def default_notebook_dir():
    """The notebook folder used when the user has not chosen one."""
    return os.path.join(config_dir(), "Notebook")


def index_root():
    """Directory under which per-notebook Whoosh indexes are stored."""
    return os.path.join(config_dir(), "index")


def _defaults():
    return {"theme": "system", "recent": [], "notebook": None,
            "font_size": 11, "pins": {}}


def load():
    """Return the config dict, always with the expected keys present."""
    cfg = _defaults()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in VALID_THEMES:
                cfg["theme"] = theme
            recent = data.get("recent")
            if isinstance(recent, list):
                cfg["recent"] = [p for p in recent if isinstance(p, str)][:MAX_RECENT]
            nb = data.get("notebook")
            if isinstance(nb, str):
                cfg["notebook"] = nb
            fs = data.get("font_size")
            if isinstance(fs, int) and 8 <= fs <= 24:
                cfg["font_size"] = fs
            pins = data.get("pins")
            if isinstance(pins, dict):
                cfg["pins"] = {k: [n for n in v if isinstance(n, str)]
                               for k, v in pins.items()
                               if isinstance(k, str) and isinstance(v, list)}
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    """Persist *cfg* (best-effort; failures are swallowed)."""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        fs = cfg.get("font_size")
        pins = cfg.get("pins")
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "system",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
            "notebook": cfg.get("notebook") if isinstance(cfg.get("notebook"), str) else None,
            "font_size": fs if isinstance(fs, int) and 8 <= fs <= 24 else 11,
            "pins": {k: [n for n in v if isinstance(n, str)]
                     for k, v in pins.items()
                     if isinstance(k, str) and isinstance(v, list)}
                    if isinstance(pins, dict) else {},
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    """The persisted theme preference: "system" (follow the OS), "light" or
    "dark".  Fresh installs return "system" so the app follows Aura live."""
    return load().get("theme", "system")


def set_theme(theme):
    if theme not in VALID_THEMES:
        return
    cfg = load()
    cfg["theme"] = theme
    save(cfg)


def get_notebook():
    return load().get("notebook")


def set_notebook(path):
    if not path:
        return
    cfg = load()
    try:
        cfg["notebook"] = os.path.abspath(path)
    except Exception:
        cfg["notebook"] = path
    save(cfg)
    add_recent(path)


def get_font_size():
    return load().get("font_size", 11)


def set_font_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return
    if not 8 <= size <= 24:
        return
    cfg = load()
    cfg["font_size"] = size
    save(cfg)


# ---------------------------------------------------------------------------
# pinned notes (stored per notebook, keyed by the notebook's absolute path;
# notes themselves stay plain .md files -- pinning is app metadata only)
# ---------------------------------------------------------------------------
def get_pins(root):
    """The ordered list of pinned note names for notebook *root*."""
    try:
        key = os.path.abspath(root)
    except Exception:
        key = str(root)
    return list(load().get("pins", {}).get(key, []))


def set_pinned(root, name, pinned):
    """Pin (or unpin) *name* in notebook *root*; returns the new pinned bool."""
    try:
        key = os.path.abspath(root)
    except Exception:
        key = str(root)
    cfg = load()
    pins = cfg.setdefault("pins", {})
    cur = [n for n in pins.get(key, []) if n != name]
    if pinned:
        cur.insert(0, name)
    if cur:
        pins[key] = cur
    else:
        pins.pop(key, None)
    save(cfg)
    return bool(pinned)


def rename_pin(root, old, new):
    """Keep a pinned note pinned across a rename."""
    if old in get_pins(root):
        set_pinned(root, old, False)
        set_pinned(root, new, True)


def get_recent():
    return load().get("recent", [])


def add_recent(path):
    """Push *path* to the front of the recent notebooks list (most-recent-first)."""
    if not path:
        return
    try:
        ap = os.path.abspath(path)
    except Exception:
        ap = path
    cfg = load()
    recent = [p for p in cfg.get("recent", []) if _abs(p) != ap]
    recent.insert(0, ap)
    cfg["recent"] = recent[:MAX_RECENT]
    save(cfg)


def clear_recent():
    cfg = load()
    cfg["recent"] = []
    save(cfg)


def _abs(p):
    try:
        return os.path.abspath(p)
    except Exception:
        return p
