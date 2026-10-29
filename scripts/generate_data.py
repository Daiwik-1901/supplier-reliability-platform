"""Thin wrapper: `python scripts/generate_data.py [--preset ...]` runs `srp generate [...]`."""

import sys

from supplier_reliability.cli import app

if __name__ == "__main__":
    app(["generate", *sys.argv[1:]], prog_name="srp")
