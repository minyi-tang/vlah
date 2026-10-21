"""The vlah command line.

Owner: W1-13 #23. See CLAUDE.md for house rules.
"""

import sys

from vlah import __version__


def main(argv: list[str] | None = None) -> int:
    """Placeholder until W1-13 lands: supports only --version."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--version"]:
        print(f"vlah {__version__}")
        return 0
    print("vlah: pre-alpha; only --version is implemented (see issue #23)", file=sys.stderr)
    return 2
