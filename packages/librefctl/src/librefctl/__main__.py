"""Module entry point for the librefctl script."""

from __future__ import annotations

import sys

from librefctl.app import main

if __name__ == "__main__":
    sys.exit(main())
