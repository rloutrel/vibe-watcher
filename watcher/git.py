"""Thin wrapper around the ``git`` CLI.

All git interactions are subprocess calls; tests mock :func:`run_git` so no
real git binaries are needed. The wrapper exposes a pure-data result object
so the watcher core stays testable.
"""

import logging
import os
import re
import subprocess

from django.conf import settings

logger = logging.getLogger(__name__)


class GitError(Exception):
    def __init__(self, message, stderr="", returncode=None, command=None):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode
        self.command = command


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


def map_path(path):
    """Translate a host path into the watcher process's view.

    In containerized deployments the host home is mounted at a different
    location (e.g. /home/romain -> /home/hostuser). VIBE_WATCHER_PATH_MAP is
    a comma-separated list of host_prefix:container_prefix pairs, applied
    longest-prefix-first.
    """
    import os

    mapping = os.environ.get("VIBE_WATCHER_PATH_MAP", "")
    if not mapping:
        return path
    pairs = []
    for item in mapping.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        host_part, _, container_part = item.partition(":")
        pairs.append((host_part.strip(), container_part.strip()))
    for host_part, container_part in sorted(pairs, key=lambda p: -len(p[0])):
        if path == host_part or path.startswith(host_part.rstrip("/") + "/"):
            return container_part + path[len(host_part):]
    return path


def list_dir_entries(path):
    """List subdirectories of the *host* path (via the container mapping).

    Returns (dirs, error): dirs are names of subdirectories sorted, plus
    ".." handling done by the caller. Paths outside the mapped mount are
    still listed when reachable — the watcher process's view is the truth.
    """
    import os

    real = map_path(path)
    if not os.path.isdir(real):
        return None, f"not a directory: {path}"
    dirs = []
    try:
        for entry in os.scandir(real):
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
                elif entry.is_symlink() and os.path.isdir(os.path.join(real, entry.name)):
                    dirs.append(entry.name)
            except OSError:
                continue
    except OSError as exc:
        return None, str(exc)
    return sorted(dirs), ""


def run_git(repo_path, *args, check=True):
    """Run a git command inside ``repo_path`` and return a GitCommandResult.

    This is the seam tests patch; no real git execution happens when mocked.
    """
    cmd = ["git", "-C", str(repo_path), *[str(a) for a in args]]
    timeout = getattr(settings, "WATCHER_GIT_TIMEOUT", 120)
    env = dict(os.environ)
    # Containers run the watcher as root while checkouts belong to the host
    # user; git >=2.35 refuses such repos ("dubuous ownership"). The watcher
    # is read-mostly (fetch / ff-only pull), so allow any ownership.
    env.setdefault("GIT_CONFIG_COUNT", "1")
    env.setdefault("GIT_CONFIG_KEY_0", "safe.directory")
    env.setdefault("GIT_CONFIG_VALUE_0", "*")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc

    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        hint = f" ({detail[0]})" if detail else ""
        raise GitError(
            f"git {' '.join(args)} failed with code {proc.returncode}{hint}",
            stderr=proc.stderr,
            returncode=proc.returncode,
            command=f"git -C {repo_path} {' '.join(str(a) for a in args)}",
        )
    return GitCommandResult(
        args, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode
    )


def https_remote_without_token(url):
    """Strip the user:token@ part from an HTTPS remote URL."""
    import re as _re

    return _re.sub(r"https://[^@/]+@", "https://", url)


def _last_fetch_error(repo_path):
    """Best-effort read of the last fetch error (run in-process cache)."""
    return _LAST_FETCH_ERROR.get(str(repo_path), "")


_LAST_FETCH_ERROR = {}


def _remember_fetch_error(repo_path, stderr):
    _LAST_FETCH_ERROR[str(repo_path)] = (stderr or "")[:500]


class GitRepo:
    """Read-only observation helpers for a local git checkout."""

    def __init__(self, path, token=None):
        self.registered_path = str(path)
        self.path = map_path(path)
        self.token = token

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
        """Remote branches for the picker (remote-only view).

        Only remote-tracking branches are listed, as short names (main,
        dev) — the watcher's purpose is tracking remote changes, so the
        choice space is what exists on the remote. ``current`` marks the
        locally checked-out branch when it matches. ``has_local`` tells
        whether a local checkout exists (used to decide auto-checkout).
        """
        # The symbolic origin/HEAD alias renders as bare "origin" with
        # --format=%(refname:short); it is not a real branch, drop it.
        remotes = [r for r in self.list_remote_branches() if "/" in r]
        locals_ = set(self.list_local_branches())
        try:
            current = self.current_branch()
        except GitError:
            current = None

        seen = set()
        choices = []
        for name in remotes:
            short = name.split("/", 1)[1]
            if short in seen:
                continue
            seen.add(short)
            choices.append(
                {
                    "name": short,
                    "remote": True,
                    "current": short == current,
                    "has_local": short in locals_,
                }
            )
        return choices

    def checkout_branch(self, branch):
        """Create a local branch tracking origin/<branch> and check it out.

        Used when the watched remote branch has no local checkout yet.
        Requires a clean working tree; raises GitError otherwise.
        """
        if not self.status_is_clean():
            raise GitError("working tree is dirty; commit or stash first")
        return run_git(
            self.path, "checkout", "-B", branch, "--track", f"origin/{branch}"
        )

    def rev_parse_head(self, check=True):
        return run_git(self.path, "rev-parse", "HEAD", check=check).stdout.strip()

    def is_repo(self):
        """True when the path holds a git repository at all."""
        return run_git(self.path, "rev-parse", "--git-dir", check=False).ok

    def has_commits(self):
        """False for a freshly init'd repo whose HEAD is unborn.

        git rev-parse HEAD on an unborn HEAD exits 128 and prints "HEAD"
        on stdout, so the return code is the reliable signal. Only call
        on paths that are repos (is_repo first), or a plain directory
        would look like an empty repo.
        """
        res = run_git(self.path, "rev-parse", "HEAD", check=False)
        return res.ok and bool(res.stdout.strip()) and res.stdout.strip() != "HEAD"

    def current_branch(self):
        # check=False: HEAD is unborn in a repo without commits; git then
        # returns "HEAD" on stdout with rc 128, which is still informative.
        res = run_git(self.path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        return (res.stdout.strip() if res.ok else "") or "HEAD"

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

    def pull_ff_only(self, branch=None):
        """Fast-forward-only pull: never creates merges or conflicts.

        Merges the already-fetched origin/* refs (the scanner always
        fetches over HTTPS before observing) instead of running `git
        pull`, which would contact the configured — often SSH — remote
        that containers cannot authenticate against.
        Raises GitError when not a fast-forward or the tree is dirty.
        """
        if branch:
            ref = f"origin/{branch}"
        else:
            up = self.upstream_name()
            ref = up if up else "origin/HEAD"
        return run_git(self.path, "merge", "--ff-only", ref)

    def fetch(self):
        """Fetch the remote so remote-tracking refs reflect reality.

        Without this, origin/* refs only move when the user runs git fetch
        manually — the watcher would compare against stale refs and miss
        remote changes. Fetch is read-only for the working tree.
        """
        run_git(self.path, "fetch", "--all", "--quiet", check=False)

    def observe(self, branch=None):
        """Collect the current observable state of the repository.

        When ``branch`` is given (Folder.branch), that branch is watched
        instead of the checked-out one: for remote refs like ``origin/main``
        the local HEAD is compared against the remote tip; for local branch
        names the upstream of that branch is used when present.
        Returns a dict with head/branch/upstream/state/commits_* fields.
        Raises GitError if the repo cannot be inspected at all.
        """
        if not self.is_repo():
            import os

            exists = os.path.exists(self.path)
            isdir = os.path.isdir(self.path)
            git_err = ""
            git_dir = os.path.join(self.path, ".git")
            has_git_dir = os.path.exists(git_dir)
            probe = run_git(self.path, "rev-parse", "--git-dir", check=False)
            if probe.returncode != 0:
                git_err = (probe.stderr or "").strip()
            raise GitError(
                "not a git repository",
                stderr=(
                    f"registered path: {self.registered_path}\n"
                    f"resolved path:   {self.path}\n"
                    f"exists: {exists}, is directory: {isdir}, .git present: {has_git_dir}\n"
                    f"git said: {git_err or '(no output)'}\n"
                    f"VIBE_WATCHER_PATH_MAP: {os.environ.get('VIBE_WATCHER_PATH_MAP') or '(unset)'}\n"
                    f"VIBE_WATCHER_PROJECTS_BASE: {os.environ.get('VIBE_WATCHER_PROJECTS_BASE') or '(unset)'}"
                ),
                command=f"git -C {self.path} rev-parse --git-dir",
            )
        if not self.has_commits():
            # Repo without any commit (fresh git init): nothing to watch yet.
            return {
                "head": "",
                "branch": branch or self.current_branch(),
                "url": self.remote_url(),
                "upstream": "",
                "state": "empty",
                "commits_ahead": 0,
                "commits_behind": 0,
                "upstream_changed": False,
            }

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
                remote_ref = run_git(
                    self.path,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{branch}",
                    check=False,
                )
                if remote_ref.ok and getattr(
                    settings, "WATCHER_AUTO_CHECKOUT", True
                ):
                    # Watched branch exists on the remote but has no local
                    # checkout: create it (tracking origin/<branch>) and
                    # check it out. Requires a clean tree; skipped silently
                    # when impossible (dirty tree, detached head...).
                    try:
                        self.checkout_branch(branch)
                    except GitError:
                        pass
                # Remote-tracking ref such as origin/main — watch its tip.
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


    def https_remote_url(self):
        """HTTPS URL for the origin remote, authenticated when possible.

        Repos are often cloned over SSH; the watcher fetches over HTTPS
        instead — no second remote, no repo config change. A token (folder
        token or VIBE_WATCHER_GIT_TOKEN) is injected when configured;
        public/open-source repos fetch fine anonymously. Returns None when
        the remote is not convertible to HTTPS.
        """
        url = self.remote_url()
        if not url:
            return None
        token = self.token or os.environ.get("VIBE_WATCHER_GIT_TOKEN", "")
        if url.startswith("https://"):
            return url.replace("https://", f"https://{token}@") if token else url
        m = re.match(r"(?:ssh://)?(?:git@)([^:/]+)[:/](.+?)(?:\.git)?$", url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
        auth = f"{token}@" if token else ""
        return f"https://{auth}{host}/{path}.git"

    ANON_FETCH_HINTS = (
        "authentication failed",
        "could not read username",
        "terminal prompts disabled",
        "403",
        "repository not found",
        "not found",
    )

    def _https_fetch(self, url):
        """Fetch url into origin/* refs. Returns (ok, anonymous)."""
        res = run_git(
            self.path,
            "fetch",
            url,
            f"+refs/heads/*:refs/remotes/origin/*",
            "--quiet",
            check=False,
        )
        _remember_fetch_error(self.path, res.stderr)
        return res.ok, "token@" not in url

    def fetch_with_token(self):
        """Fetch over HTTPS; try anonymous first, token only if needed.

        Open-source projects are fetched WITHOUT the token (a public repo
        needs no authentication, and not sending the token avoids leaking
        it to any host and avoids rate-limit coupling to the account). A
        private repo makes the anonymous fetch fail with an auth error,
        and the token fetch is retried only then.

        Returns True when an HTTPS fetch was attempted; False when the
        caller should fall back to the plain fetch (no HTTPS-convertible
        remote).
        """
        https_url = self.https_remote_url()
        if not https_url:
            return False
        token = self.token or os.environ.get("VIBE_WATCHER_GIT_TOKEN", "")
        anonymous_url = https_remote_without_token(https_url)
        ok, _ = self._https_fetch(anonymous_url)
        if not ok and token:
            lowered = _last_fetch_error(self.path) or ""
            if any(h in lowered for h in self.ANON_FETCH_HINTS):
                self._https_fetch(https_url)
        return True

    def pull_ff_only_with_token(self, branch=None):
        """Fast-forward pull over HTTPS (no SSH keys needed).

        Fetches the HTTPS remote URL into origin/* refs first (anonymous,
        token retry for private repos), then merges the fetched state with
        merge --ff-only — avoiding `git pull`, which would contact the
        configured (often SSH) remote that containers can't authenticate
        against.
        """
        self.fetch_with_token()
        return self.pull_ff_only(branch=branch)


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
        "has_remote": False,
        "no_remote_error": "",
        "error": "",
    }
    repo = GitRepo(info["path"])
    if not repo.is_repo():
        info["error"] = "not a git repository"
        return info
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


def find_repos(base_path, max_depth=3, max_results=200):
    """Recursively find git repositories under base_path.

    Skips hidden and dependency-heavy directories (node_modules, .venv,
    target, ...) and stops descending into a directory once it is itself a
    repo (submodules still count as nested repos but aren't descended).
    Returns (repos, truncated): repos is a list of absolute host paths.
    """
    import os

    real_base = map_path(base_path)
    if not os.path.isdir(real_base):
        return None, f"not a directory: {base_path}"

    skip_names = {"node_modules", "venv", ".venv", "target", "build",
                  "dist", "__pycache__", ".tox", ".cache"}
    repos = []
    truncated = False

    def walk(dir_path, depth):
        nonlocal truncated
        if len(repos) >= max_results:
            truncated = True
            return
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if len(repos) >= max_results:
                truncated = True
                return
            name = entry.name
            if name.startswith(".") and name not in (".git",):
                if name != ".git":
                    continue
            full = os.path.join(dir_path, name)
            if name == ".git":
                repos.append(dir_path)
                return
            if name in skip_names:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if depth + 1 < max_depth:
                walk(full, depth + 1)
            elif os.path.isdir(os.path.join(full, ".git")):
                repos.append(full)

    walk(real_base, 0)
    return repos[:max_results], ("truncated" if truncated else "")


def code_version():
    """Short commit hash of the running code, or 'unknown'.

    Determined from the git checkout containing this module — the bind
    mount means this reflects what the *process* sees on disk. When the
    app is served from a directory without git metadata (pip install,
    image copy), returns 'unknown'.
    """
    import os
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", os.path.dirname(__file__), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"
