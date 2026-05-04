from __future__ import annotations

import logging
from collections.abc import Callable

from src import updater

logger = logging.getLogger(__name__)


def run_update_check(
    *,
    silent: bool,
    is_sync_running: Callable[[], bool],
    notify_user: Callable[[str], None],
) -> bool:
    """Check for update + install. Returns True if app should quit."""
    try:
        result = updater.check_for_update()
    except updater.UpdateCheckError as exc:
        logger.warning("update: %s", exc)
        if not silent:
            notify_user(f"Update check failed: {exc}")
        return False
    if result is None:
        if not silent:
            notify_user("Already up to date.")
        return False
    target_sha, asset_url, sha_url = result
    if is_sync_running():
        logger.info("update: deferring (sync running) — target=%s", target_sha[:8])
        return False
    logger.info("update: installing %s", target_sha[:8])
    try:
        updater.install_update(asset_url, sha_url)
    except Exception as exc:
        logger.error("update: install failed: %s", exc)
        if not silent:
            notify_user(f"Update failed: {exc}")
        return False
    return True
