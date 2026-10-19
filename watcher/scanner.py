"""Watcher core: polls enabled folders, records snapshots/events, notifies.

This module is intentionally framework-light so tests can drive folder scans
without real git or notify-send (both are mocked at their seams).
"""

import logging
import time

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .consts import HEARTBEAT_INTERVAL
from .actions import apply_updates, docker_running, start_docker
from .git import GitError, GitRepo
from .models import Folder, NotifiedCommit, Snapshot, WatcherEvent, WatcherStatus
from .notify import get_notification_backend

logger = logging.getLogger(__name__)


def record_event(kind, message, folder=None, detail=None):
    return WatcherEvent.objects.create(
        folder=folder, kind=kind, message=message, detail=detail or {}
    )


def beat(status=None):
    """Update the watcher heartbeat so the UI can flag a stale watcher."""
    status = status or WatcherStatus.objects.first()
    if status is None:
        status = WatcherStatus.objects.create()
    status.heartbeat = timezone.now()
    status.last_run = status.heartbeat
    status.pid = _current_pid()
    status.save(update_fields=["heartbeat", "last_run", "pid"])
    return status


def _current_pid():
    import os

    return os.getpid()


def already_notified(folder, commit_hash):
    return NotifiedCommit.objects.filter(
        folder=folder, commit_hash=commit_hash
    ).exists()


def mark_notified(folder, commit_hash):
    NotifiedCommit.objects.get_or_create(folder=folder, commit_hash=commit_hash)


def scan_folder(folder, repo=None, backend=None):
    """Observe one folder and react to upstream changes.

    Returns the created Snapshot. Never raises GitError: failures are
    recorded on the snapshot and as an ``error`` event.
    """
    repo = repo or GitRepo(folder.path, token=folder.git_token or None)
    backend = backend or get_notification_backend()

    if getattr(settings, "WATCHER_FETCH_BEFORE_SCAN", True):
        try:
            if not repo.fetch_with_token():
                repo.fetch()
        except GitError as exc:
            logger.warning(
                "fetch failed, comparing against existing refs",
                extra={"folder": folder.name, "error": str(exc)},
            )

    try:
        state = repo.observe(branch=folder.branch or None)
    except GitError as exc:
        snapshot = Snapshot.objects.create(
            folder=folder,
            state="error",
            error=str(exc),
        )
        record_event("error", str(exc), folder=folder, detail={"stderr": exc.stderr})
        logger.warning(
            "folder scan failed",
            extra={"folder": folder.name, "error": str(exc)},
        )
        return snapshot

    snapshot = Snapshot.objects.create(
        folder=folder,
        head=state["head"],
        branch=state["branch"],
        upstream=state["upstream"],
        state=state["state"],
        commits_behind=state["commits_behind"],
        commits_ahead=state["commits_ahead"],
    )

    changed = state["upstream_changed"]
    if changed:
        head = state["head"]
        notification_key = f"f{folder.id}:{state['branch']}:{head}"
        if already_notified(folder, head):
            logger.info(
                "change already notified, skipping notification",
                extra={"folder": folder.name, "head": head},
            )
            record_event(
                "change",
                f"{folder.name}: upstream changed (already notified)",
                folder=folder,
                detail={"head": head, "dup": True},
            )
        else:
            action_result = apply_updates(folder, repo, state)
            if action_result["pulled"] and action_result["command_ok"]:
                follow_up = "Pulled and restarted."
            elif action_result["pulled"]:
                follow_up = "Pulled."
            elif action_result["pull_error"]:
                follow_up = f"Auto-pull failed: {action_result['pull_error']}"
            else:
                follow_up = "Run git pull to update your local copy."

            # Docker stack down? Offer to start it from the notification.
            actions = []
            stack_up = docker_running(folder)
            if stack_up is False:
                follow_up += " Docker stack is not running."
                actions = [("start-docker", "Start now")]

            summary = f"{folder.name} has updates"
            body = (
                f"{state['commits_behind']} new commit(s) on {state['upstream']}. "
                f"{follow_up}"
            )
            result = backend.notify(
                summary,
                body,
                notification_key=notification_key,
                category="email.arrived",
                actions=actions,
            )
            if stack_up is False and result.action == "start-docker":
                ok, output = start_docker(folder)
                record_event(
                    "change" if ok else "error",
                    f"{folder.name}: docker stack {'started' if ok else 'failed to start'}",
                    folder=folder,
                    detail={"output": output[-500:]},
                )
                logger.info(
                    "docker stack started from notification",
                    extra={"folder": folder.name, "ok": ok},
                )
            elif stack_up is False:
                record_event(
                    "change",
                    f"{folder.name}: docker stack not running",
                    folder=folder,
                    detail={"offered_start": True},
                )
            mark_notified(folder, head)
            record_event(
                "notification",
                f"{folder.name}: {summary}",
                folder=folder,
                detail={
                    "head": head,
                    "backend": result.backend,
                    "delivered": result.delivered,
                    "notification_id": result.notification_id,
                },
            )
            logger.info(
                "change detected, notification sent",
                extra={
                    "folder": folder.name,
                    "head": head,
                    "delivered": result.delivered,
                },
            )
            record_event(
                "change",
                f"{folder.name}: upstream changed ({state['commits_behind']} behind)",
                folder=folder,
                detail={
                    "head": head,
                    "pulled": action_result["pulled"],
                    "pull_error": action_result["pull_error"],
                    "post_pull_command": action_result["command"],
                    "command_ok": action_result["command_ok"],
                },
            )
    else:
        logger.info(
            "folder scanned, no upstream change",
            extra={"folder": folder.name, "head": state["head"]},
        )
    return snapshot


def scan_all_once(backend=None):
    """One pass over all enabled folders. Returns (snapshot_count, error_count)."""
    snapshots = errors = 0
    folders = list(Folder.objects.filter(enabled=True))
    for folder in folders:
        snapshot = scan_folder(folder, backend=backend)
        if snapshot.state == "error":
            errors += 1
        else:
            snapshots += 1

    now = timezone.now()
    status, _ = WatcherStatus.objects.get_or_create(pk=1)
    status.heartbeat = now
    status.last_run = now
    if errors == 0:
        status.last_pass = now
        status.last_error = ""
    else:
        status.last_error = "one or more folders failed to scan"
    status.scanned_folders = len(folders)
    status.iteration += 1
    status.pid = _current_pid()
    status.save()
    NotifiedCommit.prune(now=now)
    return snapshots, errors


def run_loop(interval_override=None, backend=None, max_iterations=None, sleep=None):
    """Long-lived loop over enabled folders at their poll intervals.

    The loop maintains one next-due timestamp per folder id so each folder is
    scanned on its own cadence inside a single process. ``sleep`` and
    ``max_iterations`` exist for tests.
    """
    sleep = sleep or time.sleep
    backend = backend or get_notification_backend()
    next_due = {}

    def seconds_until_due(now):
        candidates = []
        for folder in Folder.objects.filter(enabled=True):
            due = next_due.get(folder.id)
            if due is None:
                candidates.append(0.0)
            else:
                candidates.append(max(0.0, due - now))
        return min(candidates) if candidates else HEARTBEAT_INTERVAL

    iteration = 0
    logger.info("watcher loop started", extra={"pid": _current_pid()})
    while True:
        close_old_connections()
        now = time.monotonic()
        due_ids = []
        for folder in Folder.objects.filter(enabled=True):
            due = next_due.get(folder.id)
            if due is None or due <= now:
                due_ids.append(folder)
        for folder in due_ids:
            try:
                scan_folder(folder, backend=backend)
            except Exception as exc:
                logger.exception(
                    "unexpected error scanning folder",
                    extra={"folder": folder.name},
                )
                record_event("error", str(exc), folder=folder)
            next_due[folder.id] = time.monotonic() + folder.poll_interval

        try:
            beat()
        except Exception:
            logger.exception("failed to write heartbeat")

        if max_iterations is not None:
            iteration += 1
            if iteration >= max_iterations:
                break

        wait = seconds_until_due(time.monotonic())
        if interval_override is not None:
            wait = min(wait, interval_override)
        logger.debug("watcher sleeping", extra={"seconds": wait})
        sleep(wait)
