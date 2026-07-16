"""PyInstaller entry point for the packaged desktop backend.

The frozen binary accepts the normal live-clipper CLI subcommands directly
(``app``, ``pipeline``, ``service`` ...). With no arguments it defaults to
``app`` so the binary is also runnable standalone.
"""
import multiprocessing
import sys

from live_clipper.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) == 1:
        sys.argv.append("app")
    main()
