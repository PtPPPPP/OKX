from __future__ import annotations

import sys

from app.cli import app

if __name__ == "__main__":
    app(args=["download-data", *sys.argv[1:]], prog_name="python -m scripts.download_history")
