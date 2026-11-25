"""PyInstaller entry point (the package's own __main__ uses relative imports)."""

import multiprocessing

from mctracker.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
