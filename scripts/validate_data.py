"""Thin wrapper: `python scripts/validate_data.py` runs `srp validate`."""

import sys

from supplier_reliability.cli import app

if __name__ == "__main__":
    app(["validate", *sys.argv[1:]], prog_name="srp")
