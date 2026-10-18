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

    def _list_branches_cmd(self, extra_args):
        """Run ``git branch <extra_args>`` and return clean short names."""
        res = run_git(
            self.path, "branch", *extra_args, "--format=%(refname:short)", check=False
        )
        if not res.ok:
            # Fallback for very old git without --format support.
            res = run_git(self.path, "branch", *extra_args, check=False)
            branches = []
            for line in res.lines():
                name = line.strip()
                if name.startswith("*"):
                    name = name[1:].strip()
                if name and "->" not in name and name != "HEAD":
                    branches.append(name)
            return branches
        return [
            name
            for name in res.lines()
            if name and "->" not in name and name != "HEAD"
        ]

    def list_branches(self):
        """All branches known locally (git branch -a), as a list of strings.

        Local branches with slashes (e.g. vibe/xyz) appear as-is, so callers
        must not infer locality from the presence of a slash — use
        ``branch_choices()`` for the classified, deduplicated picker list.
        """
        return self._list_branches_cmd(["-a"])

    def list_local_branches(self):
        """Local branch names only (git branch)."""
        return self._list_branches_cmd([])

    def list_remote_branches(self):
        """Remote-tracking branch names (git branch -r), origin/... form."""
        return self._list_branches_cmd(["-r"])

    def has_remote(self):
        """True if the repository has at least one remote configured."""
        res = run_git(self.path, "remote", check=False)
        return res.ok and len(res.lines()) > 0

    def branch_choices(self):
        """Deduplicated branch list for the picker.

        Local and remote-tracking branches are queried separately and matched
        by name, so a local branch containing a slash (e.g.
        ``vibe/capability-spec``) is correctly classified as local. When both
        ``main`` and ``origin/main`` exist, only ``main`` is offered: the
        watcher resolves the branch's upstream automatically. A branch that
        only exists on the remote keeps its ``origin/...`` form so it can be
        watched even without a local checkout. Entries are dicts:

        - ``name``: the value to store (local branch name, or origin/... for
          remote-only branches)
        - ``remote``: True if a matching remote-tracking ref exists
        - ``current``: True for the checked-out branch
        """
        locals_ = self.list_local_branches()
        # The symbolic origin/HEAD alias renders as bare "origin" with
        # --format=%(refname:short); it is not a real branch, drop it.
        remotes = [r for r in self.list_remote_branches() if "/" in r]
        remote_names = {r.split("/", 1)[1] for r in remotes}
        current = None
        try:
            current = self.current_branch()
        except GitError:
            pass

        seen = set()
        choices = []
        for name in locals_:
            if name in seen:
                continue
            seen.add(name)
            choices.append(
                {
                    "name": name,
                    "remote": name in remote_names,
                    "current": name == current,
                }
            )
        for name in remotes:
            short = name.split("/", 1)[1]
            if short in seen:
                continue
            seen.add(short)
            choices.append(
                {
                    "name": name,
                    "remote": True,
                    "current": False,
                }
            )
        return choices

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

    def branch_tip(self, branch):
        """Resolve a local/remote branch name to its commit hash."""
        return run_git(self.path, "rev-parse", branch).stdout.strip()

    def ahead_behind_refs(self, base, compare):
        """Count commits on each side of base...compare."""
        res = run_git(
            self.path, "rev-list", "--left-right", "--count", f"{base}...{compare}"
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

    def observe(self, branch=None):
        """Collect the current observable state of the repository.

        When ``branch`` is given (Folder.branch), that branch is watched
        instead of the checked-out one: for remote refs like ``origin/main``
        the local HEAD is compared against the remote tip; for local branch
        names the upstream of that branch is used when present.
        Returns a dict with head/branch/upstream/state/commits_* fields.
        Raises GitError if the repo cannot be inspected at all.
        """
        head = self.rev_parse_head()
        current = self.current_branch()
        url = self.remote_url()
        clean = self.status_is_clean()
        state = "clean" if clean else "dirty"

        watched = branch or current
        upstream = None
        if branch:
            local = run_git(
                self.path,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
            )
            if not local.ok:
                # Not a local branch: a remote-tracking ref such as
                # origin/main — watch its tip directly.
                upstream = branch
            else:
                # Local branch name: prefer its configured upstream; fall
                # back to a matching origin/<branch> remote-tracking ref;
                # last resort the branch tip itself.
                up = run_git(
                    self.path,
                    "rev-parse",
                    "--abbrev-ref",
                    "--symbolic-full-name",
                    f"{branch}@{{upstream}}",
                    check=False,
                )
                if up.ok and up.stdout.strip():
                    upstream = up.stdout.strip()
                else:
                    origin_ref = run_git(
                        self.path,
                        "rev-parse",
                        "--verify",
                        "--quiet",
                        f"refs/remotes/origin/{branch}",
                        check=False,
                    )
                    if origin_ref.ok:
                        upstream = f"origin/{branch}"
                    else:
                        upstream = branch
        else:
            upstream = self.upstream_name()

        ahead = behind = 0
        if upstream:
            if branch and not branch.startswith(current) and upstream == branch:
                # Watching a remote ref directly: compare local tip vs remote.
                ahead, behind = self.ahead_behind_refs("HEAD", upstream)
            else:
                ahead, behind = self.ahead_behind(upstream)

        if not upstream:
            state = f"{state}:no-upstream"
        elif behind > 0:
            state = f"{state}:behind"
        elif ahead > 0:
            state = f"{state}:ahead"

        return {
            "head": head,
            "branch": watched,
            "url": url,
            "upstream": upstream or "",
            "state": state,
            "commits_ahead": ahead,
            "commits_behind": behind,
            "upstream_changed": bool(upstream and behind > 0),
        }
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


NO_REMOTE_MESSAGE = (
    "This repository has no remote. The watcher tracks remote changes, "
    "so add a remote (git remote add origin <url> && git fetch) first."
)


def expand_path(path):
    """Expand ``~`` and environment variables in a user-entered path.

    Runs in the watcher process, so ``~`` resolves against the service user's
    home (or the logged-in desktop user in systemd user mode).
    """
    import os

    return os.path.expandvars(os.path.expanduser(path.strip()))


def detect_repo(path):
    """Best-effort repo info for the quick-add flow.

    Returns a dict with resolved path, repo name, default branch, remote URL,
    and available branches. Never raises GitError — errors are reported in
    the ``error`` key so the UI can fall back to manual entry.
    """
    info = {
        "path": expand_path(path),
        "name": "",
        "branch": "",
        "url": "",
        "branches": [],
        "error": "",
    }
    repo = GitRepo(info["path"])
    try:
        current = repo.current_branch()
        info["name"] = info["path"].rstrip("/").split("/")[-1] or "folder"
        info["branch"] = current
        info["url"] = repo.remote_url()
        info["has_remote"] = repo.has_remote()
        info["branches"] = repo.list_branches()
        info["no_remote_error"] = "" if info["has_remote"] else NO_REMOTE_MESSAGE
    except GitError as exc:
        info["error"] = str(exc)
    return info
