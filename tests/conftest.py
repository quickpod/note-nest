"""Shared fixtures: an isolated notebook + config/index home in a tmp dir.

Setting ``NOTENEST_HOME`` redirects the config dir (and hence the Whoosh index
root) into the test's tmp dir, so nothing touches the real user config and every
test is hermetic.  Runs headless on Linux.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("NOTENEST_HOME", str(h))
    return h


@pytest.fixture()
def root(tmp_path):
    nb = tmp_path / "notebook"
    nb.mkdir()
    return str(nb)
