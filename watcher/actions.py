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


def docker_running(folder):
    """Check whether the folder's docker stack is up.

    A folder "has docker" when it contains a compose file; the stack is
    considered running when at least one of its containers is up. All
    subprocess interaction goes through _run_docker (mockable seam).
    """
    import os

    compose_file = find_compose_file(folder.path)
    if not compose_file:
        return None  # no docker project here
    proc = _run_docker(["compose", "ls", "--format", "json"], cwd=folder.path)
    if proc is None or proc.returncode != 0:
        return None
    import json

    try:
        projects = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(projects, dict):
        projects = [projects]
    for project in projects:
        if not isinstance(project, dict):
            continue
        config_files = str(project.get("ConfigFiles", ""))
        if os.path.basename(compose_file) in config_files or config_files == "":
            status = str(project.get("Status", ""))
            if "running" in status.lower():
                return True
    return False


def find_compose_file(path):
    import os

    candidates = [
        "compose.yaml", "compose.yml",
        "docker-compose.yaml", "docker-compose.yml",
    ]
    for candidate in candidates:
        full = os.path.join(path, candidate)
        if os.path.isfile(full):
            return full
    return None


def _run_docker(args, cwd):
    """Seam for tests: actually invokes docker when unmocked."""
    try:
        return subprocess.run(
            ["docker", *args], cwd=cwd, capture_output=True, text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def start_docker(folder):
    """Start the folder's docker stack (docker compose up -d)."""
    proc = _run_docker(["compose", "up", "-d"], cwd=folder.path)
    if proc is None:
        return False, "docker not available"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "compose up failed")[-500:]
    return True, (proc.stdout or "").strip()[-500:]
