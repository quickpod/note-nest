# NoteNest

A fast, **offline**, **100% open-source** Markdown knowledge base for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/note-nest).

> **100% AI-built and open source.** Apache-2.0.

## What it does

Write notes in Markdown with live preview, link notes together with [[wiki-links]] and see backlinks, organize with tags, and find anything with instant full-text search. A local graph connects your ideas. An Obsidian-style second brain that stores plain files you own.

## Install

Download **`NoteNest-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/note-nest) or the [GitHub release](https://github.com/quickpod/note-nest/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
pip install -r requirements.txt
python note_nest_app.py          # GUI
python -m notenest --help    # CLI
```


## Features

- **Plain Markdown files you own** — a notebook is just a folder of `.md` files; no database, no lock-in. Open it in any other editor any time.
- **Live preview** — a readable, styled render of your Markdown (headings, code, tables, lists) updates as you type.
- **`[[Wiki-links]]`** — link notes together; click a link in the preview to jump to it, and missing targets are created on follow.
- **Backlinks** — every note shows which other notes point at it, plus its own outgoing links.
- **`#tags`** — tag notes inline and filter the note list by tag.
- **Instant full-text search** — a Whoosh index over titles, bodies and tags, with snippets; updates incrementally on save.
- **Links graph** — nodes and edges over the whole notebook (via the `links` API), for a graph view.
- **Everything offline** — nothing is ever uploaded. Dark and light themes.
- **Library + CLI + GUI** — the tested `notenest` package powers a tkinter desktop app and a scriptable command line.

## CLI examples

```sh
python -m notenest --help

# Point at a notebook folder with --notebook (remembered for next time)
python -m notenest --notebook ./MyVault new "Welcome" --text "# Welcome

See [[Ideas]]. #intro"

python -m notenest --notebook ./MyVault list           # list all notes
python -m notenest --notebook ./MyVault show "Welcome"          # render to text
python -m notenest --notebook ./MyVault show "Welcome" --html   # render to HTML
python -m notenest --notebook ./MyVault search "onions"         # full-text search
python -m notenest --notebook ./MyVault backlinks "Ideas"       # who links here
python -m notenest --notebook ./MyVault tags                    # all tags + counts
python -m notenest --notebook ./MyVault reindex                 # rebuild the index
```

Commands exit cleanly with a one-line `error: ...` message (never a traceback) when something goes wrong.

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
