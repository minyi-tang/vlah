"""Entry point: python -m vlah.adapters.executor.sandbox.linux_namespace (see the package)."""

import sys

from vlah.adapters.executor.sandbox.linux_namespace import main

sys.exit(main(sys.argv[1:]))
