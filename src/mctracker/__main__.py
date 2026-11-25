import multiprocessing

from .app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # no-op unless frozen and re-spawned
    raise SystemExit(main())
