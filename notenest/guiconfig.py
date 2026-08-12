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
VALID_THEMES = ("light", "dark")


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
    return {"theme": "light", "recent": [], "notebook": None}


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
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    """Persist *cfg* (best-effort; failures are swallowed)."""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "light",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
            "notebook": cfg.get("notebook") if isinstance(cfg.get("notebook"), str) else None,
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    return load().get("theme", "light")


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
