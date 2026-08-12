"""End-to-end tests for the notenest package (deterministic, headless)."""

import pytest

from notenest import links, render, search, vault
from notenest.errors import NoteNestError


# ---------------------------------------------------------------------------
# vault CRUD
# ---------------------------------------------------------------------------
def test_write_read_round_trip(root):
    text = "# Hello\n\nBody with [[Other]] and #tag.\n"
    path = vault.write_note(root, "Hello", text)
    assert path.endswith("Hello.md")
    assert vault.read_note(root, "Hello") == text


def test_read_missing_raises(root):
    with pytest.raises(NoteNestError):
        vault.read_note(root, "Nope")


def test_list_notes(root):
    vault.write_note(root, "Beta", "b")
    vault.write_note(root, "Alpha", "a")
    vault.write_note(root, "projects/Gamma", "g")
    assert vault.list_notes(root) == ["Alpha", "Beta", "projects/Gamma"]


def test_path_traversal_rejected(root):
    with pytest.raises(NoteNestError):
        vault.note_path(root, "../escape")


def test_delete_and_rename(root):
    vault.write_note(root, "Temp", "x")
    vault.rename(root, "Temp", "Perm")
    assert vault.list_notes(root) == ["Perm"]
    vault.delete_note(root, "Perm")
    assert vault.list_notes(root) == []


# ---------------------------------------------------------------------------
# links + tags
# ---------------------------------------------------------------------------
def test_parse_links_and_tags():
    text = ("# Heading is not a tag\n"
            "See [[Inbox]] and [[Projects/Roadmap|the roadmap]].\n"
            "Tags: #todo #in-progress and code `#nothashtag` inline.\n"
            "Repeat [[Inbox]] should dedupe.\n")
    assert links.parse_links(text) == ["Inbox", "Projects/Roadmap"]
    tags = links.parse_tags(text)
    assert "todo" in tags and "in-progress" in tags
    # the leading "# Heading" (hash + space) must NOT be a tag
    assert "Heading" not in tags


def test_backlinks(root):
    vault.write_note(root, "A", "Standalone note.")
    vault.write_note(root, "B", "Points to [[A]].")
    vault.write_note(root, "C", "Also links [[A]] here.")
    vault.write_note(root, "D", "Unrelated.")
    assert links.backlinks(root, "A") == ["B", "C"]
    assert links.backlinks(root, "D") == []


def test_graph_edges(root):
    vault.write_note(root, "A", "See [[B]] and [[C]].")
    vault.write_note(root, "B", "Back to [[A]].")
    vault.write_note(root, "C", "Nothing here.")
    g = links.graph(root)
    assert g["nodes"] == ["A", "B", "C"]
    assert g["edges"] == [("A", "B"), ("A", "C"), ("B", "A")]


def test_resolve_link_create_on_follow(root):
    assert links.resolve_link(root, "Ghost") is None
    name = links.resolve_link(root, "Ghost", create=True)
    assert name == "Ghost"
    assert vault.note_exists(root, "Ghost")


def test_all_tags(root):
    vault.write_note(root, "A", "#todo #idea")
    vault.write_note(root, "B", "#todo")
    counts = links.all_tags(root)
    assert counts == {"todo": 2, "idea": 1}


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def test_render_html_has_expected_tags():
    md = "# Title\n\nSome `inline code` and:\n\n```\nblock\n```\n"
    out = render.to_html(md)
    assert "<h1" in out and "Title" in out
    assert "<code>" in out


def test_render_html_tables_and_wikilinks():
    md = ("| A | B |\n|---|---|\n| 1 | 2 |\n\nlink [[Other]] here\n")
    out = render.to_html(md)
    assert "<table>" in out
    assert 'href="note:Other"' in out


def test_to_text_strips_markup():
    md = "# Head\n\n[[Target|label]] text #tag `code`\n"
    txt = render.to_text(md)
    assert "Head" in txt and "label" in txt and "tag" in txt
    assert "[[" not in txt and "#" not in txt


# ---------------------------------------------------------------------------
# search (Whoosh)
# ---------------------------------------------------------------------------
def test_search_by_body_word(home, root):
    vault.write_note(root, "Recipes", "How to caramelize onions slowly.\n")
    vault.write_note(root, "Other", "Unrelated content about cars.\n")
    n = search.reindex(root)
    assert n == 2
    hits = search.search(root, "caramelize")
    assert [h.name for h in hits] == ["Recipes"]
    assert "CARAMELIZE" in hits[0].snippet.upper()


def test_search_by_tag(home, root):
    vault.write_note(root, "Task", "Do the thing. #urgent\n")
    vault.write_note(root, "Note", "Just a plain note.\n")
    search.reindex(root)
    hits = search.search(root, "urgent")
    assert [h.name for h in hits] == ["Task"]
    assert "urgent" in hits[0].tags
    assert "URGENT" in hits[0].snippet.upper()


def test_incremental_update_after_edit(home, root):
    vault.write_note(root, "Doc", "initial content zebra\n")
    search.reindex(root)
    assert [h.name for h in search.search(root, "zebra")] == ["Doc"]
    # edit: remove zebra, add giraffe, then incrementally update
    vault.write_note(root, "Doc", "revised content giraffe\n")
    search.update_note(root, "Doc")
    assert search.search(root, "zebra") == []
    assert [h.name for h in search.search(root, "giraffe")] == ["Doc"]


def test_search_reindex_if_missing(home, root):
    vault.write_note(root, "Fresh", "an apple a day\n")
    # no explicit reindex; search builds the index on demand
    hits = search.search(root, "apple")
    assert [h.name for h in hits] == ["Fresh"]


def test_remove_note_from_index(home, root):
    vault.write_note(root, "Gone", "ephemeral tangerine\n")
    search.reindex(root)
    assert [h.name for h in search.search(root, "tangerine")] == ["Gone"]
    search.remove_note(root, "Gone")
    assert search.search(root, "tangerine") == []
