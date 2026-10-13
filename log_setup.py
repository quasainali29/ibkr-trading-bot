"""Logging: console (concise) + daily file under logs/ (detailed)."""

import logging
from datetime import datetime


def setup_logging(log_dir, verbose=False):
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"bot_{datetime.now():%Y-%m-%d}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(threadName)-12s %(name)s: %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s",
                                      "%H:%M:%S"))
    root.addHandler(ch)

    # ibapi's own logger is extremely chatty at DEBUG/INFO.
    logging.getLogger("ibapi").setLevel(logging.WARNING)
    return path
