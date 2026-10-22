"""Entry point: python -m vlah.adapters.harness.claude_code.hook (run by Claude Code)."""

import os
import sys

from vlah.adapters.harness.claude_code.hook import main

sys.exit(main(sys.stdin, sys.stdout, os.environ))
