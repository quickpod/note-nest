"""Error types for notenest."""


class NoteNestError(Exception):
    """Raised for any recoverable failure in a notenest operation.

    All public functions raise this (and only this) on failure so callers
    -- the CLI and the GUI -- have a single exception type to catch and can
    show a clean message instead of a raw traceback.
    """
