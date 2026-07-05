"""Entry point for the PyInstaller binary — just runs the engram CLI."""
from engram.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
