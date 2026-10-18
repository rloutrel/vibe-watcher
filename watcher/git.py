"""Thin wrapper around the ``git`` CLI.

All git interactions are subprocess calls; tests mock :func:`run_git` so no
real git binaries are needed. The wrapper exposes a pure-data result object
so the watcher core stays testable.
"""

import logging
import subprocess

from django.conf import settings

logger = logging.getLogger(__name__)


class GitError(Exception):
    def __init__(self, message, stderr="", returncode=None):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class GitCommandResult:
    def __init__(self, args, stdout="", stderr="", returncode=0):
        self.args = args
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    @property
    def ok(self):
        return self.returncode == 0

    def lines(self):
        return [line for line in self.stdout.splitlines() if line.strip()]


def run_git(repo_path, *args, check=True):
    """Run a git command inside ``repo_path`` and return a GitCommandResult.

    This is the seam tests patch; no real git execution happens when mocked.
    """
    cmd = ["git", "-C", str(repo_path), *[str(a) for a in args]]
    timeout = getattr(settings, "WATCHER_GIT_TIMEOUT", 120)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc

    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed with code {proc.returncode}",
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    return GitCommandResult(
        args, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode
    )


class GitRepo:
    """Read-only observation helpers for a local git checkout."""

    def __init__(self, path):
        self.path = path

    def rev_parse_head(self):
        return run_git(self.path, "rev-parse", "HEAD").stdout.strip()

    def current_branch(self):
        res = run_git(self.path, "rev-parse", "--abbrev-ref", "HEAD")
        return res.stdout.strip()

    def remote_url(self):
        res = run_git(self.path, "remote", "get-url", "origin", check=False)
        return res.stdout.strip()

    def status_is_clean(self):
        res = run_git(self.path, "status", "--porcelain", check=False)
        return len(res.lines()) == 0

    def upstream_name(self):
        res = run_git(
            self.path,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            check=False,
        )
        if not res.ok:
            return None
        return res.stdout.strip() or None

    def ahead_behind(self, upstream):
        res = run_git(
            self.path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
        )
        parts = res.stdout.split()
        if len(parts) != 2:
            return 0, 0
        return int(parts[0]), int(parts[1])

    def local_only_commits(self, upstream, limit=10):
        res = run_git(
            self.path, "rev-list", "--max-count", limit, f"HEAD..{upstream}"
        )
        return res.lines()

    def safe_to_pull(self):
        return self.status_is_clean()

    def observe(self):
        """Collect the current observable state of the repository.

        Returns a dict with head/branch/upstream/state/commits_* fields.
        Raises GitError if the repo cannot be inspected at all.
        """
        head = self.rev_parse_head()
        branch = self.current_branch()
        url = self.remote_url()
        clean = self.status_is_clean()
        upstream = self.upstream_name()

        state = "clean" if clean else "dirty"
        ahead = behind = 0
        if upstream:
            ahead, behind = self.ahead_behind(upstream)

        if not upstream:
            state = f"{state}:no-upstream"
        elif behind > 0:
            state = f"{state}:behind"
        elif ahead > 0:
            state = f"{state}:ahead"

        return {
            "head": head,
            "branch": branch,
            "url": url,
            "upstream": upstream or "",
            "state": state,
            "commits_ahead": ahead,
            "commits_behind": behind,
            "upstream_changed": bool(upstream and behind > 0),
        }
