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

    def observe(self):
        self.calls += 1
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
        self.assertEqual(backend.notifications[0][2], f"f{folder.id}:def456")
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

