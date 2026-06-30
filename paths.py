"""Resolve where the bridge keeps its writable runtime files.

The code can live on any disk (it is never written to), but the *churny* files —
the topic + /del state map (rewritten on every relayed message) and the logs —
constantly hit the disk and can wear an SSD. Set ``MAX2TG_DATA_DIR`` to put them
on another disk (e.g. a spinning HDD). Defaults to the app directory, so nothing
changes unless you opt in (Docker keeps using MAX2TG_STATE_PATH as before).
"""
import os
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    """Directory for writable runtime files. Created on demand; falls back to the
    app directory if MAX2TG_DATA_DIR is set but cannot be created."""
    configured = os.environ.get("MAX2TG_DATA_DIR")
    if not configured:
        return _APP_DIR
    target = Path(configured)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _APP_DIR
    return target


def data_path(filename: str) -> Path:
    """Absolute path for a writable runtime file under the data dir."""
    return data_dir() / filename
