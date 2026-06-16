"""Disposable AOC instance for previewing changes: port 8081, temp database.

Never touches the real data/ folder - safe to stop and delete at any time.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must be set before importing aoc, which reads it at import time.
os.environ["AOC_DATA_DIR"] = os.path.join(tempfile.gettempdir(), "aoc-preview-test")

from waitress import serve

from aoc import create_app

print("Disposable AOC test instance: http://localhost:8081 (temp database)")
serve(create_app(), host="127.0.0.1", port=8081)
