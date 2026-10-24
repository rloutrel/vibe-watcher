"""Automatic local persistence of the folder configuration.

The service keeps a JSON copy of its configuration on disk (default:
``<base_dir>/watcher-config.json``, configurable via VIBE_WATCHER_CONFIG_PATH).
Every folder create/update/delete/enable-toggle rewrites the file, and the
watcher restores from it on startup when the database has no folders — so a
wiped or lost database self-heals on the next ``watch_folders`` run.
"""

import json
import logging
import os
import tempfile

from django.conf import settings

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "watcher-config.json"


def config_path():
    """Location of the on-disk config (env VIBE_WATCHER_CONFIG_PATH wins)."""
    return os.environ.get("VIBE_WATCHER_CONFIG_PATH") or str(
        settings.BASE_DIR / CONFIG_FILENAME
    )


def folders_payload():
    from .models import Folder

    return {
        "app": "vibe-watcher",
        "version": 1,
        "folders": [f.to_dict() for f in Folder.objects.all().order_by("id")],
    }


def save_config():
    """Write the current folder configuration to disk (atomic replace).

    No-op unless VIBE_WATCHER_CONFIG_PATH is set: the on-disk copy is a
    service-deployment feature, and writing files into arbitrary working
    directories (e.g. during tests or ad-hoc shell usage) would be noise.
    """
    if not os.environ.get("VIBE_WATCHER_CONFIG_PATH"):
        return True
    path = config_path()
    payload = json.dumps(folders_payload(), indent=2)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".watcher-config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.exception("failed to persist watcher config to %s", path)
        return False
    logger.info(
        "watcher config persisted",
        extra={"path": path, "folders": len(payload and json.loads(payload)["folders"])},
    )
    return True


def load_config():
    """Read the on-disk config; returns the folders list or None if absent."""
    path = config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.exception("failed to read watcher config from %s", path)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("folders"), list):
        logger.warning("watcher config at %s has unexpected shape, ignoring", path)
        return None
    return payload["folders"]


def restore_if_needed():
    """Recreate folders from the on-disk config when the database is empty.

    Called at watcher startup: if the database was wiped (or is fresh) but a
    local config file exists, the folder configuration is recovered from it.
    Returns the number of folders restored.
    """
    from .consts import DEFAULT_POLL_INTERVAL, MAX_INTERVAL
    from .models import Folder

    if Folder.objects.exists():
        return 0

    entries = load_config()
    if not entries:
        return 0

    restored = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        path = entry["path"]
        try:
            interval = max(1, min(int(entry.get("poll_interval") or DEFAULT_POLL_INTERVAL), MAX_INTERVAL))
        except (TypeError, ValueError):
            interval = DEFAULT_POLL_INTERVAL
        Folder.objects.create(
            name=entry.get("name") or path.rstrip("/").split("/")[-1] or "folder",
            path=path,
            url=entry.get("url") or "",
            branch=entry.get("branch") or "",
            poll_interval=interval,
            enabled=bool(entry.get("enabled", True)),
        )
        restored += 1

    if restored:
        logger.info(
            "watcher config restored from disk",
            extra={"path": config_path(), "restored": restored},
        )
    return restored
