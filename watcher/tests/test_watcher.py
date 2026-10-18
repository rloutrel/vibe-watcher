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
        self.observed_branchs = []

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

    def notify(self, summary, body, notification_key=None, category=None):
        self.notifications.append((summary, body, notification_key, category))
        from watcher.notify import NotificationResult

        return NotificationResult(self.name, delivered=True, notification_id=42)


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
            if cmd == ("branch", "-a", "--format=%(refname:short)"):
                return git_out("main\ndev\norigin/main\norigin/feature")
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
            if cmd == ("branch", "-a", "--format=%(refname:short)"):
                return git_out("main\norigin/main\norigin/release")
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
            if cmd == ("branch", "-a", "--format=%(refname:short)"):
                return git_out("main\ndev\norigin/main")
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
            if cmd == ("branch", "-a", "--format=%(refname:short)"):
                return git_out("main")
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
