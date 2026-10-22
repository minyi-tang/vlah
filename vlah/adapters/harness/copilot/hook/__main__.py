"""Entry point: python -m vlah.adapters.harness.copilot.hook (run by Copilot CLI)."""

import os
import sys

from vlah.adapters.harness.copilot.hook import main

sys.exit(main(sys.stdin, sys.stdout, os.environ))
