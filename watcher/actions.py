"""Automatic actions the watcher takes when a remote change is detected.

Separated from the scanner so the pull / post-pull-command pipeline can be
tested with fully mocked subprocess calls.
"""

import logging
import subprocess

from django.conf import settings

from .git import GitError

logger = logging.getLogger(__name__)


def run_post_pull_command(folder, command):
    """Run the configured shell command inside the folder (mockable seam)."""
    timeout = getattr(settings, "WATCHER_GIT_TIMEOUT", 120) * 5
    argv = ["sh", "-c", command]
    try:
        proc = subprocess.run(
            argv, cwd=folder.path, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"post-pull command timed out after {timeout}s", ""
    except FileNotFoundError as exc:
        return False, str(exc), ""
    return (
        proc.returncode == 0,
        proc.stdout,
        proc.stderr,
    )


def apply_updates(folder, repo, state):
    """Pull and run the post-pull command when configured.

    Returns a dict describing what happened:
    - pulled: whether a git pull --ff-only was attempted and succeeded
    - pull_error: pull failure reason (diverged/dirty tree), if any
    - command: the post-pull command that ran (or was attempted)
    - command_ok / command_output / command_error
    """
    result = {
        "pulled": False,
        "pull_error": "",
        "command": "",
        "command_ok": False,
        "command_output": "",
        "command_error": "",
    }

    if not folder.auto_pull:
        return result

    try:
        repo.pull_ff_only(branch=folder.branch or None)
        result["pulled"] = True
    except GitError as exc:
        result["pull_error"] = str(exc)
        logger.warning(
            "auto-pull failed",
            extra={"folder": folder.name, "error": str(exc), "stderr": exc.stderr},
        )
        return result

    command = (folder.post_pull_command or "").strip()
    if not command:
        return result

    result["command"] = command
    ok, output, err = run_post_pull_command(folder, command)
    result["command_ok"] = ok
    result["command_output"] = output[-2000:]
    result["command_error"] = err[-2000:]
    if ok:
        logger.info(
            "post-pull command succeeded",
            extra={"folder": folder.name, "command": command},
        )
    else:
        logger.warning(
            "post-pull command failed",
            extra={
                "folder": folder.name,
                "command": command,
                "error": err[-500:],
            },
        )
    return result
