r"""Markdown rendering + a plain-text extractor.

:func:`to_html` renders a note to HTML with the ``fenced_code``, ``tables`` and
``toc`` extensions enabled -- used by the GUI preview and the ``show --html``
CLI command.  ``[[wiki-links]]`` are turned into anchors first so they survive
into the HTML (and the GUI can make them clickable).

:func:`to_text` strips a note down to indexable plain text (no markup, no
wiki-link/tag punctuation) for the Whoosh index and for a readable console
preview.  Both functions are pure and deterministic.
"""

from __future__ import annotations

import html
import re

from . import links

_MD_EXTENSIONS = ["fenced_code", "tables", "toc"]

# capture fenced/inline code so we do not mangle it in the plain-text pass
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_TAG_LINE_TAGS = re.compile(r"<[^>]+>")


def _wiki_to_html(text):
    """Replace ``[[Target|label]]`` with an anchor, escaping the visible text."""
    def repl(m):
        inner = m.group(1)
        if "|" in inner:
            target, label = inner.split("|", 1)
        else:
            target = label = inner
        target = target.strip()
        label = label.strip() or target
        return (f'<a class="wikilink" href="note:{html.escape(target)}">'
                f'{html.escape(label)}</a>')

    return re.sub(r"\[\[([^\[\]]+?)\]\]", repl, text or "")


def to_html(text):
    """Render note *text* to an HTML fragment (never raises)."""
    import markdown  # imported lazily so the storage layer stays dep-free

    prepared = _wiki_to_html(text or "")
    try:
        md = markdown.Markdown(extensions=_MD_EXTENSIONS, output_format="html5")
        return md.convert(prepared)
    except Exception:
        # last-resort: escape and wrap so we never surface a traceback
        return "<pre>" + html.escape(text or "") + "</pre>"


def to_text(text):
    """Extract readable plain text from note *text* for indexing/preview.

    Wiki-link labels and tag words are kept (so they remain searchable) but the
    surrounding ``[[ ]]`` / ``#`` punctuation and Markdown markup are removed.
    """
    if not text:
        return ""

    # keep wiki-link display text
    def wiki_repl(m):
        inner = m.group(1)
        return inner.split("|", 1)[-1].strip() if "|" in inner else inner.strip()

    s = re.sub(r"\[\[([^\[\]]+?)\]\]", wiki_repl, text)
    # keep tag words (drop the leading #)
    s = links._TAG_RE.sub(lambda m: m.group(1), s)

    # fenced code -> its contents; inline code -> its contents
    s = _CODE_FENCE_RE.sub(lambda m: m.group(0).strip("`"), s)
    s = _INLINE_CODE_RE.sub(r"\1", s)

    # strip common inline/block markdown markers
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)      # images -> alt
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)   # heading hashes
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)         # blockquotes
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)         # bullet markers
    s = re.sub(r"[*_~]{1,3}([^*_~]+)[*_~]{1,3}", r"\1", s)  # emphasis
    s = _TAG_LINE_TAGS.sub("", s)                          # any stray html

    # collapse whitespace, keep line structure light
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)
