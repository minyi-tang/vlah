"""Errors every adapter of a port raises for the same situations."""


class NotSupportedError(Exception):
    """The adapter does not offer this optional capability (for example checkpoints)."""


class PushRejectedError(PermissionError):
    """A commit came without the push credential; only the tool server holds it (D68)."""


class AppendOnlyViolationError(Exception):
    """A merge would modify or delete a file that already exists (D31)."""


class ReadPermissionError(PermissionError):
    """The reader's role may not read this object, for example private data (D63)."""
