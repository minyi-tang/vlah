"""Entry point: python -m vlah.adapters.harness.claude_code.runner <claude command...>."""

import os
import sys
from pathlib import Path

from vlah.adapters.harness.claude_code.runner import main

sys.exit(main(sys.argv[1:], os.environ, Path.cwd(), sys.stdout.buffer))
