"""Wings of Canada AOC - entry point.

    python app.py            -> serves on http://localhost:8080 (waitress)
    set PORT=5000 first to use a different port.
"""
import os

from waitress import serve

from aoc import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print(f"Wings of Canada AOC running on http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    serve(app, host=host, port=port)
