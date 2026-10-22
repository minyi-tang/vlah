"""Entry point: python -m vlah.adapters.harness.copilot.runner <copilot command...>."""

import os
import sys
from pathlib import Path

from vlah.adapters.harness.copilot.runner import main

sys.exit(main(sys.argv[1:], os.environ, Path.cwd()))
