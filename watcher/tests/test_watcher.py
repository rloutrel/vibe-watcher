from io import StringIO
from pathlib import Path
from unittest import mock

import django.test as django_test
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from watcher.git import GitCommandResult, GitError, GitRepo, run_git
from watcher.models import (
    Folder,
    NotifiedCommit,
    NotificationState,
    Snapshot,
    WatcherEvent,
    WatcherStatus,
)
from watcher.notify import DbusNotificationBackend, NullNotificationBackend, get_notification_backend
from watcher import scanner


def git_out(stdout="", stderr="", returncode=0):
    return GitCommandResult([], stdout=stdout, stderr=stderr, returncode=returncode)


class RepoFixture:
    """Builds Folder instances pointing at fake paths; git is always mocked."""

    def make_folder(self, **kwargs):
        defaults = {
            "name": "repo-a",
            "path": "/tmp/fake/repo-a",
            "poll_interval": 300,
            "enabled": True,
        }
        defaults.update(kwargs)
        return Folder.objects.create(**defaults)


def observe_result(
    head="abc123",
    branch="main",
    url="git@github.com:me/repo-a.git",
    upstream="origin/main",
    ahead=0,
    behind=0,
    clean=True,
):
    return {
        "head": head,
        "branch": branch,
        "url": url,
        "upstream": upstream,
        "upstream_changed": bool(behind > 0),
        "state": ("clean" if clean else "dirty")
        + (":behind" if behind > 0 else (":ahead" if ahead > 0 else "")),
        "commits_ahead": ahead,
        "commits_behind": behind,
    }


class FakeRepo:
    def __init__(self, result=None, error=None):
        self.result = result or observe_result()
        self.error = error
        self.calls = 0
        self.fetched = 0
        self.observed_branchs = []

    def fetch(self):
        self.fetched += 1

    def observe(self, branch=None):
        self.calls += 1
        self.observed_branchs.append(branch)
        if self.error:
            raise self.error
        return self.result


class FakeBackend:
    def __init__(self):
        self.notifications = []
        self.name = "fake"

    def notify(self, summary, body, notification_key=None, category=None,
               actions=None):
        self.notifications.append((summary, body, notification_key, category))
        self.actions = actions or []
        from watcher.notify import NotificationResult

        return NotificationResult(
            self.name, delivered=True, notification_id=42,
            action=getattr(self, "action_to_return", None),
        )


class RunGitTests(TestCase):
    """run_git must shell out to git with the right argv (mocked subprocess)."""

    @mock.patch("watcher.git.subprocess")
    def test_run_git_argv(self, subprocess_mod):
        subprocess_mod.run.return_value = mock.Mock(
            returncode=0, stdout="hash\n", stderr=""
        )
        result = run_git("/tmp/fake/repo-a", "rev-parse", "HEAD")
        subprocess_mod.run.assert_called_once()
        args, kwargs = subprocess_mod.run.call_args
        self.assertEqual(
            args[0], ["git", "-C", "/tmp/fake/repo-a", "rev-parse", "HEAD"]
        )
        self.assertEqual(result.stdout, "hash\n")
        self.assertTrue(result.ok)

    @mock.patch("watcher.git.subprocess")
    def test_run_git_raises_on_failure(self, subprocess_mod):
        subprocess_mod.run.return_value = mock.Mock(
            returncode=128, stdout="", stderr="fatal: not a git repository"
        )
        with self.assertRaises(GitError):
            run_git("/tmp/fake/repo-a", "status")


class GitRepoTests(TestCase):
    @mock.patch("watcher.git.run_git")
    def test_observe_clean(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("rev-parse", "HEAD"):
                return git_out("abc123")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote", "get-url", "origin"):
                return git_out("git@github.com:me/repo-a.git")
            if cmd == ("status", "--porcelain"):
                return git_out("")
            if "@{upstream}" in cmd:
                return git_out("origin/main")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        state = repo.observe()
        self.assertEqual(state["head"], "abc123")
        self.assertEqual(state["branch"], "main")
        self.assertEqual(state["upstream"], "origin/main")
        self.assertEqual(state["state"], "clean")
        self.assertEqual(state["commits_behind"], 0)

    @mock.patch("watcher.git.run_git")
    def test_observe_behind(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("rev-parse", "HEAD"):
                return git_out("abc123")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote", "get-url", "origin"):
                return git_out("git@github.com:me/repo-a.git")
            if cmd == ("status", "--porcelain"):
                return git_out("")
            if "@{upstream}" in cmd:
                return git_out("origin/main")
            if cmd[0] == "rev-list" and any("..." in part for part in cmd):
                return git_out("0\t3\n")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        state = repo.observe()
        self.assertEqual(state["commits_behind"], 3)
        self.assertEqual(state["commits_ahead"], 0)
        self.assertTrue(state["upstream_changed"])
        self.assertEqual(state["state"], "clean:behind")


class ScanFolderTests(TestCase, RepoFixture):
    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_no_change(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(observe_result())
        folder = self.make_folder()
        backend = FakeBackend()
        snapshot = scanner.scan_folder(folder, backend=backend)
        self.assertEqual(snapshot.state, "clean")
        self.assertEqual(snapshot.commits_behind, 0)
        self.assertEqual(backend.notifications, [])
        self.assertEqual(Snapshot.objects.count(), 1)

    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_detects_change_and_notifies(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=2, head="def456"))
        folder = self.make_folder()
        backend = FakeBackend()
        snapshot = scanner.scan_folder(folder, backend=backend)
        self.assertEqual(snapshot.state, "clean:behind")
        self.assertEqual(snapshot.commits_behind, 2)
        self.assertEqual(len(backend.notifications), 1)
        self.assertEqual(backend.notifications[0][2], f"f{folder.id}:main:def456")
        self.assertTrue(
            NotifiedCommit.objects.filter(folder=folder, commit_hash="def456").exists()
        )

    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_does_not_renotify_same_commit(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=2, head="def456"))
        folder = self.make_folder()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        scanner.scan_folder(folder, backend=backend)
        self.assertEqual(len(backend.notifications), 1)

    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_error_recorded(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(error=GitError("boom", stderr="oops"))
        folder = self.make_folder()
        snapshot = scanner.scan_folder(folder, backend=FakeBackend())
        self.assertEqual(snapshot.state, "error")
        self.assertIn("boom", snapshot.error)
        self.assertTrue(
            WatcherEvent.objects.filter(kind="error", folder=folder).exists()
        )


class HeartbeatTests(TestCase, RepoFixture):
    def test_beat_creates_and_updates(self):
        status = scanner.beat()
        self.assertIsNotNone(status.heartbeat)
        first = status.heartbeat
        status2 = scanner.beat(status=status)
        self.assertEqual(status2.pk, status.pk)
        self.assertGreaterEqual(status2.heartbeat, first)

    def test_scan_all_once_updates_status(self):
        self.make_folder(name="a", path="/tmp/a")
        self.make_folder(name="b", path="/tmp/b", enabled=False)
        with mock.patch.object(scanner, "scan_folder") as scan_mock:
            scan_mock.return_value = Snapshot(state="clean")
            snapshots, errors = scanner.scan_all_once()
            self.assertEqual(snapshots, 1)
            self.assertEqual(errors, 0)
            self.assertEqual(scan_mock.call_count, 1)
        status = WatcherStatus.objects.first()
        self.assertIsNotNone(status)
        self.assertIsNotNone(status.heartbeat)
        self.assertEqual(status.scanned_folders, 1)
        self.assertEqual(status.iteration, 1)

    def test_run_loop_respects_intervals(self):
        self.make_folder(name="a", path="/tmp/a", poll_interval=300)
        self.make_folder(name="b", path="/tmp/b", poll_interval=10)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with mock.patch.object(scanner, "scan_folder") as scan_mock, \
                mock.patch.object(scanner, "beat") as beat_mock:
            scan_mock.return_value = Snapshot(state="clean")
            with self.assertRaises(KeyboardInterrupt):
                scanner.run_loop(sleep=fake_sleep)
            self.assertEqual(scan_mock.call_count, 2)
            self.assertTrue(beat_mock.called)

    def test_run_loop_max_iterations(self):
        self.make_folder(name="a", path="/tmp/a", poll_interval=300)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(scanner, "scan_folder") as scan_mock, \
                mock.patch.object(scanner, "beat") as beat_mock:
            scan_mock.return_value = Snapshot(state="clean")
            scanner.run_loop(max_iterations=3, sleep=fake_sleep)
            # Folders are only re-scanned when due again, so one scan in 3
            # iterations, but the loop still sleeps between iterations.
            self.assertEqual(scan_mock.call_count, 1)
            self.assertTrue(beat_mock.called)
            self.assertEqual(len(sleeps), 2)

    def test_run_loop_new_folder_scanned_immediately(self):
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        self.make_folder(name="a", path="/tmp/a", poll_interval=300)
        with mock.patch.object(scanner, "scan_folder") as scan_mock, \
                mock.patch.object(scanner, "beat"):
            scan_mock.return_value = Snapshot(state="clean")
            scanner.run_loop(max_iterations=1, sleep=fake_sleep)
            self.assertEqual(scan_mock.call_count, 1)
            scanner.run_loop(max_iterations=1, sleep=fake_sleep)
            self.assertEqual(scan_mock.call_count, 2)


class NotificationIdReuseTests(TestCase, RepoFixture):
    def _fake_proc(self, stdout="123\n", returncode=0, stderr=""):
        proc = mock.Mock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_first_notify_persists_id(self, send_mock):
        send_mock.return_value = self._fake_proc("77\n")
        backend = DbusNotificationBackend()
        result = backend.notify("s", "b", notification_key="f1:abc")
        send_mock.assert_called_once()
        argv = send_mock.call_args[0][0]
        self.assertNotIn("-r", argv)
        self.assertTrue(result.delivered)
        self.assertEqual(result.notification_id, 77)
        state = NotificationState.objects.get(app_name="vibe-watcher", notification_key="f1:abc")
        self.assertEqual(state.notification_id, 77)

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_second_notify_reuses_id(self, send_mock):
        NotificationState.objects.create(
            app_name="vibe-watcher",
            notification_key="f1:abc",
            notification_id=77,
            notified_at=timezone.now(),
        )
        send_mock.return_value = self._fake_proc("77\n")
        backend = DbusNotificationBackend()
        result = backend.notify("s", "b", notification_key="f1:abc")
        argv = send_mock.call_args[0][0]
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-r") + 1], "77")
        self.assertEqual(result.notification_id, 77)

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_reuse_window_expiry(self, send_mock):
        NotificationState.objects.create(
            app_name="vibe-watcher",
            notification_key="f1:abc",
            notification_id=77,
        )
        NotificationState.objects.filter(notification_key="f1:abc").update(
            notified_at=timezone.now() - timezone.timedelta(hours=2)
        )
        send_mock.return_value = self._fake_proc("99\n")
        backend = DbusNotificationBackend()
        backend.notify("s", "b", notification_key="f1:abc")
        argv = send_mock.call_args[0][0]
        self.assertNotIn("-r", argv)

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_fresh_key_gets_new_id(self, send_mock):
        send_mock.return_value = self._fake_proc("55\n")
        backend = DbusNotificationBackend()
        result = backend.notify("s", "b", notification_key="f2:xyz")
        self.assertEqual(result.notification_id, 55)

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_notify_send_failure_is_not_delivered(self, send_mock):
        send_mock.return_value = self._fake_proc("", returncode=1, stderr="no session bus")
        backend = DbusNotificationBackend()
        result = backend.notify("s", "b", notification_key="f3:q")
        self.assertFalse(result.delivered)

    def test_null_backend_never_delivers(self):
        backend = NullNotificationBackend()
        result = backend.notify("s", "b")
        self.assertFalse(result.delivered)

    def test_get_backend_dispatch(self):
        self.assertIsInstance(get_notification_backend("none"), NullNotificationBackend)
        self.assertIsInstance(get_notification_backend("dbus"), DbusNotificationBackend)
        with self.assertRaises(ValueError):
            get_notification_backend("carrier-pigeon")


class WatchFoldersCommandTests(TestCase, RepoFixture):
    @mock.patch.object(scanner, "scan_all_once")
    def test_once_runs_single_pass(self, scan_all_mock):
        scan_all_mock.return_value = (2, 0)
        out = StringIO()
        call_command("watch_folders", "--once", stdout=out)
        self.assertTrue(scan_all_mock.called)
        self.assertIn("Scanned 2 folder(s)", out.getvalue())

    @mock.patch.object(scanner, "run_loop")
    def test_default_runs_loop(self, run_loop_mock):
        call_command("watch_folders")
        self.assertTrue(run_loop_mock.called)


class UITests(TestCase, RepoFixture):
    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user("alice", password="pw")
        self.client = django_test.Client()

    def test_index_requires_login(self):
        response = self.client.get(reverse("watcher:index"))
        self.assertEqual(response.status_code, 302)

    def test_index_shows_watcher_status(self):
        self.client.login(username="alice", password="pw")
        response = self.client.get(reverse("watcher:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never run")

        status = scanner.beat()
        response = self.client.get(reverse("watcher:index"))
        self.assertContains(response, "Running")

    def test_index_flags_stale(self):
        self.client.login(username="alice", password="pw")
        status = WatcherStatus.objects.create()
        status.heartbeat = timezone.now() - timezone.timedelta(hours=1)
        status.save()
        response = self.client.get(reverse("watcher:index"))
        self.assertContains(response, "Stale heartbeat")

    def test_folder_crud(self):
        self.client.login(username="alice", password="pw")
        response = self.client.post(
            reverse("watcher:folder-create"),
            {
                "name": "repo-x",
                "path": "/tmp/x",
                "url": "",
                "poll_interval": 60,
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        folder = Folder.objects.get(name="repo-x")
        response = self.client.post(
            reverse("watcher:folder-update", args=[folder.pk]),
            {
                "name": "repo-x",
                "path": "/tmp/x",
                "url": "",
                "poll_interval": 120,
                "enabled": "on",
            },
        )
        folder.refresh_from_db()
        self.assertEqual(folder.poll_interval, 120)
        self.client.post(reverse("watcher:folder-delete", args=[folder.pk]))
        self.assertFalse(Folder.objects.filter(name="repo-x").exists())



class BranchSelectionTests(TestCase, RepoFixture):
    @mock.patch("watcher.git.run_git")
    def test_list_branches_format(self, run_git_mock):
        run_git_mock.return_value = git_out("main\ndev\nremotes/origin/main\nremotes/origin/dev")
        repo = GitRepo("/tmp/fake/repo-a")
        branches = repo.list_branches()
        self.assertEqual(
            branches, ["main", "dev", "remotes/origin/main", "remotes/origin/dev"]
        )
        cmd = run_git_mock.call_args[0][1:]
        self.assertEqual(cmd[0], "branch")
        self.assertIn("-a", cmd)

    @mock.patch("watcher.git.run_git")
    def test_list_branches_fallback(self, run_git_mock):
        res = GitCommandResult([], stdout="", stderr="", returncode=128)
        res2 = GitCommandResult(
            [], stdout="* main\n  dev\n  remotes/origin/main", stderr="", returncode=0
        )
        run_git_mock.side_effect = [res, res2]
        repo = GitRepo("/tmp/fake/repo-a")
        branches = repo.list_branches()
        self.assertEqual(branches, ["main", "dev", "remotes/origin/main"])

    @mock.patch("watcher.git.run_git")
    def test_observe_uses_folder_branch(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("rev-parse", "HEAD"):
                return git_out("abc123")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote", "get-url", "origin"):
                return git_out("git@github.com:me/repo-a.git")
            if cmd == ("status", "--porcelain"):
                return git_out("")
            if cmd == ("rev-parse", "--verify", "--quiet", "refs/heads/origin/dev"):
                return GitCommandResult([], stdout="", stderr="", returncode=1)
            if cmd[-1] == "origin/dev":
                return git_out("def999")
            if cmd[0] == "rev-list" and cmd[1] == "--left-right":
                return git_out("0\t2\n")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        state = repo.observe(branch="origin/dev")
        self.assertEqual(state["branch"], "origin/dev")
        self.assertEqual(state["upstream"], "origin/dev")
        self.assertEqual(state["commits_behind"], 2)
        self.assertTrue(state["upstream_changed"])

    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_folder_passes_branch(self, GitRepoMock):
        fake = FakeRepo(observe_result(behind=1, head="cafe00", branch="dev"))
        GitRepoMock.return_value = fake
        folder = self.make_folder(branch="dev")
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        self.assertEqual(fake.observed_branchs, ["dev"])
        self.assertEqual(backend.notifications[0][2], f"f{folder.id}:dev:cafe00")

    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_folder_default_branch_when_empty(self, GitRepoMock):
        fake = FakeRepo(observe_result())
        GitRepoMock.return_value = fake
        folder = self.make_folder(branch="")
        scanner.scan_folder(folder, backend=FakeBackend())
        self.assertEqual(fake.observed_branchs, [None])

    @mock.patch("watcher.views.GitRepo")
    def test_folder_branches_endpoint(self, GitRepoViewMock):
        repo = GitRepoViewMock.return_value
        repo.list_branches.return_value = ["main", "dev", "origin/main"]
        repo.has_remote.return_value = True
        repo.branch_choices.return_value = [
            {"name": "main", "remote": True, "current": True},
            {"name": "dev", "remote": False, "current": False},
        ]
        folder = self.make_folder()
        from django.contrib.auth.models import User

        User.objects.create_user("bob", password="pw")
        self.client.login(username="bob", password="pw")
        response = self.client.get(reverse("watcher:folder-branches", args=[folder.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["has_remote"], True)
        self.assertEqual(data["no_remote_error"], "")
        self.assertEqual(
            data["branches"],
            [
                {"name": "main", "remote": True, "current": True, "disabled": False},
                {"name": "dev", "remote": False, "current": False, "disabled": True},
            ],
        )

    @mock.patch("watcher.views.GitRepo")
    def test_branches_for_path_endpoint(self, GitRepoViewMock):
        repo = GitRepoViewMock.return_value
        repo.has_remote.return_value = False
        repo.branch_choices.return_value = [
            {"name": "main", "remote": False, "current": True}
        ]
        from django.contrib.auth.models import User

        User.objects.create_user("carol", password="pw")
        self.client.login(username="carol", password="pw")
        response = self.client.get(
            reverse("watcher:branches-for-path"), {"path": "/tmp/fake/repo-a"}
        )
        self.assertEqual(response.status_code, 200)
        GitRepoViewMock.assert_called_with("/tmp/fake/repo-a")
        data = response.json()
        self.assertEqual(data["has_remote"], False)
        self.assertIn("no remote", data["no_remote_error"])
        self.assertTrue(data["branches"][0]["disabled"])

    def test_branches_for_path_requires_path(self):
        from django.contrib.auth.models import User

        User.objects.create_user("dave", password="pw")
        self.client.login(username="dave", password="pw")
        response = self.client.get(reverse("watcher:branches-for-path"))
        self.assertEqual(response.status_code, 400)


class AuthTemplateTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user("erin", password="pw")
        self.client = django_test.Client()

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Log in")

    def test_login_flow(self):
        response = self.client.post(
            reverse("login"),
            {"username": "erin", "password": "pw"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("watcher:index"))
        self.assertEqual(response.status_code, 200)

    def test_logout_redirects_to_index(self):
        self.client.login(username="erin", password="pw")
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("watcher:index"))


class QuickAddTests(TestCase, RepoFixture):
    def test_expand_path_tilde(self):
        from watcher.git import expand_path
        import os

        self.assertEqual(expand_path("/abs/path"), "/abs/path")
        self.assertEqual(expand_path(" ~/code/x "), os.path.expanduser("~/code/x"))

    @mock.patch("watcher.git.GitRepo")
    def test_detect_repo(self, GitRepoMock):
        from watcher.git import detect_repo

        repo = GitRepoMock.return_value
        repo.current_branch.return_value = "main"
        repo.remote_url.return_value = "https://github.com/me/my-repo.git"
        repo.list_branches.return_value = ["main", "origin/dev"]
        info = detect_repo("~/code/my-repo")
        import os

        self.assertEqual(info["path"], os.path.expanduser("~/code/my-repo"))
        self.assertEqual(info["name"], "my-repo")
        self.assertEqual(info["branch"], "main")
        self.assertEqual(info["url"], "https://github.com/me/my-repo.git")
        self.assertEqual(info["branches"], ["main", "origin/dev"])
        self.assertEqual(info["error"], "")

    @mock.patch("watcher.git.GitRepo")
    def test_detect_repo_error_is_reported(self, GitRepoMock):
        from watcher.git import detect_repo

        repo = GitRepoMock.return_value
        repo.current_branch.side_effect = GitError("not a git repository")
        info = detect_repo("/tmp/not-a-repo")
        self.assertEqual(info["error"], "not a git repository")
        self.assertEqual(info["branches"], [])

    @mock.patch("watcher.views.detect_repo")
    def test_detect_endpoint(self, detect_mock):
        from django.contrib.auth.models import User

        detect_mock.return_value = {
            "path": "/home/me/code/my-repo",
            "name": "my-repo",
            "branch": "main",
            "url": "https://github.com/me/my-repo",
            "branches": ["main"],
            "error": "",
        }
        User.objects.create_user("frank", password="pw")
        self.client.login(username="frank", password="pw")
        response = self.client.get(
            reverse("watcher:folder-detect"), {"path": "~/code/my-repo"}
        )
        self.assertEqual(response.status_code, 200)
        detect_mock.assert_called_with("~/code/my-repo")
        self.assertEqual(response.json()["name"], "my-repo")

    def test_detect_endpoint_requires_path(self):
        from django.contrib.auth.models import User

        User.objects.create_user("gina", password="pw")
        self.client.login(username="gina", password="pw")
        response = self.client.get(reverse("watcher:folder-detect"))
        self.assertEqual(response.status_code, 400)

    @mock.patch("watcher.forms.expand_path")
    def test_form_expands_tilde_and_derives_name(self, expand_mock):
        expand_mock.side_effect = lambda p: "/home/me/code/" + p.strip("~/")
        from watcher.forms import FolderForm

        form = FolderForm(
            data={
                "name": "",
                "path": "~/code/my-repo",
                "url": "",
                "branch": "",
                "poll_interval": 300,
                "enabled": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["path"], "/home/me/code/code/my-repo")
        self.assertEqual(form.cleaned_data["name"], "my-repo")


class BranchChoicesTests(TestCase, RepoFixture):
    @mock.patch("watcher.git.run_git")
    def test_dedupe_local_and_remote(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main\ndev")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("origin/main\norigin/feature")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("origin")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        choices = repo.branch_choices()
        names = [c["name"] for c in choices]
        self.assertEqual(names, ["main", "dev", "origin/feature"])
        by_name = {c["name"]: c for c in choices}
        self.assertTrue(by_name["main"]["remote"])
        self.assertFalse(by_name["dev"]["remote"])
        self.assertTrue(by_name["main"]["current"])
        self.assertFalse(by_name["dev"]["current"])

    @mock.patch("watcher.git.run_git")
    def test_remote_only_branch_kept(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("origin/main\norigin/release")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("origin")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        names = [c["name"] for c in repo.branch_choices()]
        self.assertEqual(names, ["main", "origin/release"])

    @mock.patch("watcher.git.run_git")
    def test_no_remote_marks_all_disabled(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "-a", "--format=%(refname:short)"):
                return git_out("main\ndev")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        self.assertFalse(repo.has_remote())
        choices = repo.branch_choices()
        self.assertTrue(all(not c["remote"] for c in choices))

    @mock.patch("watcher.git.run_git")
    def test_payload_disables_remoteless_branches(self, run_git_mock):
        from watcher.views import _branch_payload

        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main\ndev")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("origin/main")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("origin")
            return git_out()

        run_git_mock.side_effect = side_effect
        payload = _branch_payload(GitRepo("/tmp/fake/repo-a"))
        self.assertTrue(payload["has_remote"])
        self.assertEqual(payload["no_remote_error"], "")
        by_name = {b["name"]: b for b in payload["branches"]}
        self.assertFalse(by_name["main"]["disabled"])
        self.assertTrue(by_name["dev"]["disabled"])
        self.assertNotIn("origin/main", by_name)

    @mock.patch("watcher.git.run_git")
    def test_payload_no_remote_error(self, run_git_mock):
        from watcher.views import _branch_payload

        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("")
            return git_out()

        run_git_mock.side_effect = side_effect
        payload = _branch_payload(GitRepo("/tmp/fake/repo-a"))
        self.assertFalse(payload["has_remote"])
        self.assertIn("no remote", payload["no_remote_error"])
        self.assertTrue(all(b["disabled"] for b in payload["branches"]))


    @mock.patch("watcher.git.run_git")
    def test_slash_named_local_branch_classified_local(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main\nvibe/capability-spec-9d99e8")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("origin/main\norigin/vibe/capability-spec-9d99e8")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("vibe/capability-spec-9d99e8")
            if cmd == ("remote",):
                return git_out("origin")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        choices = repo.branch_choices()
        self.assertEqual(
            [c["name"] for c in choices],
            ["main", "vibe/capability-spec-9d99e8"],
        )
        by_name = {c["name"]: c for c in choices}
        self.assertTrue(by_name["vibe/capability-spec-9d99e8"]["remote"])
        self.assertTrue(by_name["vibe/capability-spec-9d99e8"]["current"])

    @mock.patch("watcher.git.run_git")
    def test_remote_only_slash_branch_offered_for_watch(self, run_git_mock):
        # A remote branch with no local checkout must still be selectable.
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("branch", "--format=%(refname:short)"):
                return git_out("main")
            if cmd == ("branch", "-r", "--format=%(refname:short)"):
                return git_out("origin/main\norigin/feature/x")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote",):
                return git_out("origin")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        choices = repo.branch_choices()
        self.assertEqual([c["name"] for c in choices], ["main", "origin/feature/x"])
        by_name = {c["name"]: c for c in choices}
        self.assertTrue(by_name["origin/feature/x"]["remote"])


    @mock.patch("watcher.git.run_git")
    def test_observe_slash_local_branch_prefers_origin_fallback(self, run_git_mock):
        # Local branch vibe/x with no upstream config but an origin/vibe/x
        # remote-tracking ref: observe must compare against origin/vibe/x.
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            if cmd == ("rev-parse", "HEAD"):
                return git_out("abc123")
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote", "get-url", "origin"):
                return git_out("git@github.com:me/repo-a.git")
            if cmd == ("status", "--porcelain"):
                return git_out("")
            if cmd == ("rev-parse", "--verify", "--quiet", "refs/heads/vibe/x"):
                return git_out("abc123")
            if cmd == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "vibe/x@{upstream}"):
                return GitCommandResult([], stdout="", stderr="", returncode=128)
            if cmd == ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/vibe/x"):
                return git_out("def999")
            if cmd[0] == "rev-list" and cmd[1] == "--left-right":
                return git_out("0\t1\n")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fake/repo-a")
        state = repo.observe(branch="vibe/x")
        self.assertEqual(state["branch"], "vibe/x")
        self.assertEqual(state["upstream"], "origin/vibe/x")
        self.assertEqual(state["commits_behind"], 1)
        self.assertTrue(state["upstream_changed"])


class ToggleExportImportTests(TestCase, RepoFixture):
    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.create_user("helen", password="pw")
        self.client.login(username="helen", password="pw")

    def test_toggle_flips_enabled(self):
        folder = self.make_folder(enabled=True)
        response = self.client.post(reverse("watcher:folder-toggle", args=[folder.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["enabled"], False)
        folder.refresh_from_db()
        self.assertFalse(folder.enabled)

        response = self.client.post(reverse("watcher:folder-toggle", args=[folder.pk]))
        self.assertEqual(response.json()["enabled"], True)
        folder.refresh_from_db()
        self.assertTrue(folder.enabled)

    def test_toggle_requires_post(self):
        folder = self.make_folder()
        response = self.client.get(reverse("watcher:folder-toggle", args=[folder.pk]))
        self.assertEqual(response.status_code, 405)

    def test_export_returns_json_config(self):
        self.make_folder(name="a", path="/tmp/a", branch="main", enabled=False)
        self.make_folder(name="b", path="/tmp/b")
        response = self.client.get(reverse("watcher:folders-export"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"], "application/json"
        )
        payload = response.json()
        self.assertEqual(payload["app"], "vibe-watcher")
        self.assertEqual(len(payload["folders"]), 2)
        by_path = {f["path"]: f for f in payload["folders"]}
        self.assertEqual(by_path["/tmp/a"]["branch"], "main")
        self.assertFalse(by_path["/tmp/a"]["enabled"])
        self.assertTrue(by_path["/tmp/b"]["enabled"])

    def test_import_restores_folders(self):
        import io
        import json as jsonlib

        config = {
            "app": "vibe-watcher",
            "version": 1,
            "folders": [
                {"name": "a", "path": "/tmp/a", "url": "", "branch": "main",
                 "poll_interval": 60, "enabled": True},
                {"name": "b", "path": "/tmp/b", "url": "", "branch": "",
                 "poll_interval": 30, "enabled": False},
            ],
        }
        upload = io.BytesIO(jsonlib.dumps(config).encode())
        upload.name = "config.json"
        response = self.client.post(reverse("watcher:folders-import"), {"config": upload})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data, {"created": 2, "updated": 0, "skipped": 0})
        self.assertEqual(Folder.objects.count(), 2)
        a = Folder.objects.get(path="/tmp/a")
        self.assertEqual(a.branch, "main")
        b = Folder.objects.get(path="/tmp/b")
        self.assertFalse(b.enabled)

    def test_import_updates_existing_folders(self):
        import io
        import json as jsonlib

        self.make_folder(name="old", path="/tmp/a", poll_interval=300)
        config = {
            "folders": [
                {"name": "new", "path": "/tmp/a", "branch": "dev",
                 "poll_interval": 45, "enabled": False},
            ],
        }
        upload = io.BytesIO(jsonlib.dumps(config).encode())
        upload.name = "config.json"
        response = self.client.post(reverse("watcher:folders-import"), {"config": upload})
        data = response.json()
        self.assertEqual(data, {"created": 0, "updated": 1, "skipped": 0})
        folder = Folder.objects.get(path="/tmp/a")
        self.assertEqual(folder.name, "new")
        self.assertEqual(folder.branch, "dev")
        self.assertEqual(folder.poll_interval, 45)
        self.assertFalse(folder.enabled)

    def test_import_rejects_garbage(self):
        import io

        upload = io.BytesIO(b"not json at all")
        upload.name = "config.json"
        response = self.client.post(reverse("watcher:folders-import"), {"config": upload})
        self.assertEqual(response.status_code, 400)

        upload2 = io.BytesIO(b'{"nope": true}')
        upload2.name = "config.json"
        response = self.client.post(reverse("watcher:folders-import"), {"config": upload2})
        self.assertEqual(response.status_code, 400)

        response = self.client.post(reverse("watcher:folders-import"), {})
        self.assertEqual(response.status_code, 400)

    def test_index_renders_switch_and_icons(self):
        self.make_folder()
        response = self.client.get(reverse("watcher:index"))
        self.assertContains(response, "switch on")
        self.assertContains(response, "toggle-form")
        self.assertContains(response, "icon-btn")
        self.assertContains(response, "folders/export/")
        self.assertContains(response, "folders/import/")


class LocalPersistTests(TestCase, RepoFixture):
    def setUp(self):
        super().setUp()
        import tempfile
        import os

        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "watcher-config.json")
        self._orig = os.environ.get("VIBE_WATCHER_CONFIG_PATH")
        os.environ["VIBE_WATCHER_CONFIG_PATH"] = self.config_path

    def tearDown(self):
        import os

        if self._orig is None:
            os.environ.pop("VIBE_WATCHER_CONFIG_PATH", None)
        else:
            os.environ["VIBE_WATCHER_CONFIG_PATH"] = self._orig
        super().tearDown()

    def test_save_on_folder_change(self):
        import json
        import os

        self.assertFalse(os.path.exists(self.config_path))
        folder = self.make_folder(name="a", path="/tmp/a", branch="main")
        self.assertTrue(os.path.exists(self.config_path))
        with open(self.config_path) as fh:
            payload = json.load(fh)
        self.assertEqual(len(payload["folders"]), 1)
        self.assertEqual(payload["folders"][0]["path"], "/tmp/a")
        self.assertEqual(payload["folders"][0]["branch"], "main")

        folder.enabled = False
        folder.save()
        with open(self.config_path) as fh:
            payload = json.load(fh)
        self.assertFalse(payload["folders"][0]["enabled"])

        folder.delete()
        with open(self.config_path) as fh:
            payload = json.load(fh)
        self.assertEqual(payload["folders"], [])

    def test_toggle_persists(self):
        import json

        from django.contrib.auth.models import User

        User.objects.create_user("ivy", password="pw")
        self.client.login(username="ivy", password="pw")
        folder = self.make_folder()
        self.client.post(reverse("watcher:folder-toggle", args=[folder.pk]))
        with open(self.config_path) as fh:
            payload = json.load(fh)
        self.assertFalse(payload["folders"][0]["enabled"])

    def test_restore_when_db_empty(self):
        import json
        import os

        config = {
            "app": "vibe-watcher",
            "version": 1,
            "folders": [
                {"name": "a", "path": "/tmp/a", "url": "", "branch": "main",
                 "poll_interval": 60, "enabled": True},
                {"name": "b", "path": "/tmp/b", "url": "", "branch": "",
                 "poll_interval": 30, "enabled": False},
            ],
        }
        with open(self.config_path, "w") as fh:
            json.dump(config, fh)

        from watcher import persist

        restored = persist.restore_if_needed()
        self.assertEqual(restored, 2)
        self.assertEqual(Folder.objects.count(), 2)
        a = Folder.objects.get(path="/tmp/a")
        self.assertEqual(a.branch, "main")
        self.assertTrue(a.enabled)
        b = Folder.objects.get(path="/tmp/b")
        self.assertFalse(b.enabled)

    def test_no_restore_when_db_has_folders(self):
        import json
        import os

        self.make_folder(name="existing", path="/tmp/existing")
        config = {"folders": [{"name": "x", "path": "/tmp/x", "poll_interval": 30, "enabled": True}]}
        with open(self.config_path, "w") as fh:
            json.dump(config, fh)

        from watcher import persist

        self.assertEqual(persist.restore_if_needed(), 0)
        self.assertEqual(Folder.objects.count(), 1)
        self.assertEqual(Folder.objects.first().path, "/tmp/existing")

    def test_no_restore_without_config_file(self):
        from watcher import persist

        self.assertEqual(persist.restore_if_needed(), 0)
        self.assertEqual(Folder.objects.count(), 0)

    def test_restore_ignores_corrupt_config(self):
        import os

        with open(self.config_path, "w") as fh:
            fh.write("{broken json")
        from watcher import persist

        self.assertEqual(persist.restore_if_needed(), 0)

    def test_command_restores_on_startup(self):
        import json
        import os

        config = {"folders": [{"name": "r", "path": "/tmp/r", "branch": "dev",
                               "poll_interval": 30, "enabled": True}]}
        with open(self.config_path, "w") as fh:
            json.dump(config, fh)
        out = StringIO()
        call_command("watch_folders", "--once", stdout=out)
        self.assertIn("Recovered 1 folder(s)", out.getvalue())
        self.assertTrue(Folder.objects.filter(path="/tmp/r").exists())


    @override_settings(WATCHER_FETCH_BEFORE_SCAN=True)
    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_fetches_before_observe(self, GitRepoMock):
        fake = FakeRepo(observe_result(behind=1, head="beef00"))
        GitRepoMock.return_value = fake
        folder = self.make_folder()
        scanner.scan_folder(folder, backend=FakeBackend())
        self.assertEqual(fake.fetched, 1)

    @override_settings(WATCHER_FETCH_BEFORE_SCAN=False)
    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_fetch_can_be_disabled(self, GitRepoMock):
        fake = FakeRepo(observe_result())
        GitRepoMock.return_value = fake
        folder = self.make_folder()
        scanner.scan_folder(folder, backend=FakeBackend())
        self.assertEqual(fake.fetched, 0)

    @override_settings(WATCHER_FETCH_BEFORE_SCAN=True)
    @mock.patch("watcher.scanner.GitRepo")
    def test_scan_survives_fetch_error(self, GitRepoMock):
        fake = FakeRepo(observe_result(behind=1, head="beef00"))

        def broken_fetch():
            raise GitError("network down")

        fake.fetch = broken_fetch
        GitRepoMock.return_value = fake
        folder = self.make_folder()
        snapshot = scanner.scan_folder(folder, backend=FakeBackend())
        self.assertNotEqual(snapshot.state, "error")


class CheckNowTests(TestCase, RepoFixture):
    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.create_user("jane", password="pw")
        self.client.login(username="jane", password="pw")

    @mock.patch("watcher.scanner.GitRepo")
    def test_check_now_returns_fresh_snapshot(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(
            observe_result(behind=3, head="feed00", branch="main")
        )
        folder = self.make_folder()
        response = self.client.post(
            reverse("watcher:folder-check-now", args=[folder.pk])
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "clean:behind")
        self.assertEqual(data["commits_behind"], 3)
        self.assertEqual(Snapshot.objects.filter(folder=folder).count(), 1)

    @mock.patch("watcher.scanner.GitRepo")
    def test_check_now_records_error_state(self, GitRepoMock):
        GitRepoMock.return_value = FakeRepo(error=GitError("no repo"))
        folder = self.make_folder()
        response = self.client.post(
            reverse("watcher:folder-check-now", args=[folder.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "error")

    def test_check_now_requires_post(self):
        folder = self.make_folder()
        response = self.client.get(reverse("watcher:folder-check-now", args=[folder.pk]))
        self.assertEqual(response.status_code, 405)

    def test_index_has_check_now_button(self):
        self.make_folder()
        response = self.client.get(reverse("watcher:index"))
        self.assertContains(response, "check-form")
        self.assertContains(response, "title=\"Check now\"")


class AutoPullTests(TestCase, RepoFixture):
    def make_folder_auto(self, command="docker compose up --build -d"):
        return self.make_folder(
            name="auto",
            path="/tmp/auto",
            auto_pull=True,
            post_pull_command=command,
        )

    @mock.patch("watcher.actions.run_post_pull_command")
    @mock.patch("watcher.scanner.GitRepo")
    def test_pull_and_command_run_on_change(self, GitRepoMock, run_cmd_mock):
        fake = FakeRepo(observe_result(behind=2, head="cafe11", branch="main"))
        fake.pull_ff_only = mock.Mock()
        GitRepoMock.return_value = fake
        run_cmd_mock.return_value = (True, "Container started", "")
        folder = self.make_folder_auto()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        fake.pull_ff_only.assert_called_once()
        run_cmd_mock.assert_called_once()
        called_folder, called_command = run_cmd_mock.call_args[0]
        self.assertEqual(called_command, "docker compose up --build -d")
        self.assertIn("Pulled and restarted.", backend.notifications[0][1])
        event = WatcherEvent.objects.filter(kind="change").first()
        self.assertTrue(event.detail["pulled"])
        self.assertTrue(event.detail["command_ok"])

    @mock.patch("watcher.actions.run_post_pull_command")
    @mock.patch("watcher.scanner.GitRepo")
    def test_pull_failure_reports_and_skips_command(self, GitRepoMock, run_cmd_mock):
        fake = FakeRepo(observe_result(behind=2, head="cafe11"))
        fake.pull_ff_only = mock.Mock(side_effect=GitError("not a fast-forward"))
        GitRepoMock.return_value = fake
        folder = self.make_folder_auto()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        fake.pull_ff_only.assert_called_once()
        run_cmd_mock.assert_not_called()
        self.assertIn("Auto-pull failed", backend.notifications[0][1])
        event = WatcherEvent.objects.filter(kind="change").first()
        self.assertFalse(event.detail["pulled"])
        self.assertTrue(event.detail["pull_error"])

    @mock.patch("watcher.actions.run_post_pull_command")
    @mock.patch("watcher.scanner.GitRepo")
    def test_no_pull_when_auto_pull_off(self, GitRepoMock, run_cmd_mock):
        fake = FakeRepo(observe_result(behind=2, head="cafe11"))
        fake.pull_ff_only = mock.Mock()
        GitRepoMock.return_value = fake
        folder = self.make_folder(auto_pull=False, post_pull_command="echo hi")
        scanner.scan_folder(folder, backend=FakeBackend())
        fake.pull_ff_only.assert_not_called()
        run_cmd_mock.assert_not_called()

    @mock.patch("watcher.actions.run_post_pull_command")
    @mock.patch("watcher.scanner.GitRepo")
    def test_pull_without_command(self, GitRepoMock, run_cmd_mock):
        fake = FakeRepo(observe_result(behind=2, head="cafe11"))
        fake.pull_ff_only = mock.Mock()
        GitRepoMock.return_value = fake
        folder = self.make_folder(auto_pull=True, post_pull_command="")
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        fake.pull_ff_only.assert_called_once()
        run_cmd_mock.assert_not_called()
        self.assertIn("Pulled.", backend.notifications[0][1])

    @mock.patch("watcher.actions.run_post_pull_command")
    @mock.patch("watcher.scanner.GitRepo")
    def test_command_failure_reported(self, GitRepoMock, run_cmd_mock):
        fake = FakeRepo(observe_result(behind=2, head="cafe11"))
        fake.pull_ff_only = mock.Mock()
        GitRepoMock.return_value = fake
        run_cmd_mock.return_value = (False, "", "compose: command not found")
        folder = self.make_folder_auto()
        scanner.scan_folder(folder, backend=FakeBackend())
        event = WatcherEvent.objects.filter(kind="change").first()
        self.assertTrue(event.detail["pulled"])
        self.assertFalse(event.detail["command_ok"])
        self.assertIn("compose", event.detail["post_pull_command"])

    @mock.patch("watcher.actions.subprocess")
    def test_run_post_pull_command_invocation(self, subprocess_mod):
        from watcher.actions import run_post_pull_command

        subprocess_mod.run.return_value = mock.Mock(
            returncode=0, stdout="ok out", stderr=""
        )
        folder = self.make_folder(path="/tmp/with-compose")
        ok, out, err = run_post_pull_command(folder, "docker compose up --build -d")
        self.assertTrue(ok)
        args, kwargs = subprocess_mod.run.call_args
        self.assertEqual(args[0][:3], ["sh", "-c", "docker compose up --build -d"])
        self.assertEqual(kwargs["cwd"], "/tmp/with-compose")


class DockerStartOfferTests(TestCase, RepoFixture):
    def setUp(self):
        import tempfile
        import os

        self.tmpdir = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmpdir, "proj")
        os.makedirs(self.repo)
        with open(os.path.join(self.repo, "compose.yaml"), "w") as fh:
            fh.write("services: {web: {image: nginx}}\n")

    def make_folder(self, **kw):
        kw.setdefault("path", self.repo)
        kw.setdefault("name", "proj")
        return super().make_folder(**kw)

    @mock.patch("watcher.actions._run_docker")
    def test_docker_running_states(self, docker_mock):
        from watcher.actions import docker_running

        def mkproc(rc, out):
            proc = mock.Mock()
            proc.returncode = rc
            proc.stdout = out
            proc.stderr = ""
            return proc

        docker_mock.return_value = mkproc(
            0, '[{"Name":"proj","Status":"running(3)","ConfigFiles":"compose.yaml"}]'
        )
        folder = self.make_folder()
        self.assertIs(docker_running(folder), True)

        docker_mock.return_value = mkproc(
            0, '[{"Name":"proj","Status":"exited(0)","ConfigFiles":"compose.yaml"}]'
        )
        self.assertIs(docker_running(folder), False)

    @mock.patch("watcher.actions._run_docker")
    def test_docker_running_without_compose_file(self, docker_mock):
        import os
        import shutil

        from watcher.actions import docker_running

        os.remove(os.path.join(self.repo, "compose.yaml"))
        folder = self.make_folder()
        self.assertIsNone(docker_running(folder))
        docker_mock.assert_not_called()

    @mock.patch("watcher.scanner.start_docker")
    @mock.patch("watcher.scanner.docker_running", return_value=False)
    @mock.patch("watcher.scanner.GitRepo")
    def test_change_with_stopped_stack_offers_start(self, GitRepoMock, up_mock,
                                                    start_mock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=1, head="dd00"))
        start_mock.return_value = (True, "started")
        folder = self.make_folder()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        self.assertEqual(backend.actions, [("start-docker", "Start now")])
        self.assertIn("Docker stack is not running", backend.notifications[0][1])

    @mock.patch("watcher.scanner.start_docker")
    @mock.patch("watcher.scanner.docker_running", return_value=False)
    @mock.patch("watcher.scanner.GitRepo")
    def test_start_action_starts_the_stack(self, GitRepoMock, up_mock, start_mock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=1, head="dd00"))
        start_mock.return_value = (True, "Container started")
        folder = self.make_folder()
        backend = FakeBackend()
        backend.action_to_return = "start-docker"
        scanner.scan_folder(folder, backend=backend)
        start_mock.assert_called_once()
        event = WatcherEvent.objects.filter(
            message__contains="docker stack started"
        ).first()
        self.assertIsNotNone(event)

    @mock.patch("watcher.scanner.start_docker")
    @mock.patch("watcher.scanner.docker_running", return_value=True)
    @mock.patch("watcher.scanner.GitRepo")
    def test_running_stack_no_offer(self, GitRepoMock, up_mock, start_mock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=1, head="dd00"))
        folder = self.make_folder()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        self.assertEqual(backend.actions, [])
        start_mock.assert_not_called()

    @mock.patch("watcher.scanner.docker_running", return_value=None)
    @mock.patch("watcher.scanner.GitRepo")
    def test_no_compose_no_offer(self, GitRepoMock, up_mock):
        GitRepoMock.return_value = FakeRepo(observe_result(behind=1, head="dd00"))
        folder = self.make_folder()
        backend = FakeBackend()
        scanner.scan_folder(folder, backend=backend)
        self.assertEqual(backend.actions, [])

    @mock.patch("watcher.actions._run_docker")
    def test_start_docker_invocation(self, docker_mock):
        from watcher.actions import start_docker

        proc = mock.Mock(returncode=0, stdout="started", stderr="")
        docker_mock.return_value = proc
        folder = self.make_folder()
        ok, out = start_docker(folder)
        self.assertTrue(ok)
        args, kwargs = docker_mock.call_args
        self.assertEqual(args[0][:3], ["compose", "up", "-d"])
        self.assertEqual(kwargs["cwd"], self.repo)

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_notify_with_actions_argv(self, send_mock):
        send_mock.return_value = mock.Mock(
            returncode=0, stdout="77\nstart-docker\n", stderr=""
        )
        backend = DbusNotificationBackend()
        result = backend.notify(
            "s", "b", notification_key="dk1",
            actions=[("start-docker", "Start now")],
        )
        argv = send_mock.call_args[0][0]
        self.assertIn("-A", argv)
        self.assertEqual(argv[argv.index("-A") + 1], "start-docker=Start now")
        self.assertTrue(result.delivered)
        self.assertEqual(result.action, "start-docker")

    @mock.patch.object(DbusNotificationBackend, "_run_notify_send")
    def test_notify_action_closed_is_none(self, send_mock):
        send_mock.return_value = mock.Mock(
            returncode=0, stdout="78\nclosed\n", stderr=""
        )
        backend = DbusNotificationBackend()
        result = backend.notify(
            "s", "b", notification_key="dk2",
            actions=[("start-docker", "Start now")],
        )
        self.assertIsNone(result.action)


class EmptyRepoTests(TestCase, RepoFixture):
    @mock.patch("watcher.git.run_git")
    def test_observe_empty_repo_returns_empty_state(self, run_git_mock):
        def side_effect(*args, **kwargs):
            cmd = args[1:]
            check = kwargs.get("check", True)
            if cmd == ("rev-parse", "HEAD"):
                if check:
                    raise GitError("rev-parse HEAD failed")
                return GitCommandResult([], stdout="", stderr="", returncode=128)
            if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
                return git_out("main")
            if cmd == ("remote", "get-url", "origin"):
                return git_out("")
            return git_out()

        run_git_mock.side_effect = side_effect
        repo = GitRepo("/tmp/fresh")
        state = repo.observe()
        self.assertEqual(state["state"], "empty")
        self.assertEqual(state["head"], "")
        self.assertEqual(state["branch"], "main")
        self.assertFalse(state["upstream_changed"])

    def test_error_message_includes_git_hint(self):
        with self.assertRaises(GitError) as ctx:
            run_git("/tmp/definitely-not-a-repo", "rev-parse", "HEAD")
        self.assertIn("code 128", str(ctx.exception))
        self.assertIn("fatal", str(ctx.exception).lower())


class PathMapTests(TestCase, RepoFixture):
    def tearDown(self):
        import os

        os.environ.pop("VIBE_WATCHER_PATH_MAP", None)
        super().tearDown()

    def test_map_path_translates_prefixes(self):
        from watcher.git import map_path
        import os

        os.environ["VIBE_WATCHER_PATH_MAP"] = "/home/romain:/home/hostuser"
        self.assertEqual(map_path("/home/romain/Basil/repo"), "/home/hostuser/Basil/repo")
        self.assertEqual(map_path("/home/romain"), "/home/hostuser")
        self.assertEqual(map_path("/etc/other"), "/etc/other")

    def test_map_path_longest_prefix_wins(self):
        from watcher.git import map_path
        import os

        os.environ["VIBE_WATCHER_PATH_MAP"] = (
            "/home/romain:/home/hostuser,/home/romain/Basil:/data/basil"
        )
        self.assertEqual(map_path("/home/romain/Basil/repo"), "/data/basil/repo")
        self.assertEqual(map_path("/home/romain/other"), "/home/hostuser/other")

    def test_map_path_disabled(self):
        from watcher.git import map_path
        import os

        os.environ.pop("VIBE_WATCHER_PATH_MAP", None)
        self.assertEqual(map_path("/home/romain/repo"), "/home/romain/repo")

    @mock.patch("watcher.git.run_git")
    def test_gitrepo_maps_path(self, run_git_mock):
        import os

        os.environ["VIBE_WATCHER_PATH_MAP"] = "/home/romain:/home/hostuser"
        run_git_mock.return_value = git_out("")
        GitRepo("/home/romain/Basil/repo").status_is_clean()
        self.assertEqual(run_git_mock.call_args[0][0], "/home/hostuser/Basil/repo")

    @mock.patch("watcher.actions.subprocess")
    def test_post_pull_command_runs_in_mapped_path(self, subprocess_mod):
        import os

        from watcher.actions import run_post_pull_command

        os.environ["VIBE_WATCHER_PATH_MAP"] = "/home/romain:/home/hostuser"
        subprocess_mod.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        folder = self.make_folder(path="/home/romain/Basil/repo")
        run_post_pull_command(folder, "docker compose up -d")
        self.assertEqual(
            subprocess_mod.run.call_args[1]["cwd"], "/home/hostuser/Basil/repo"
        )
