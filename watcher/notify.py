"""Desktop notification backends.

The default backend targets the D-Bus session bus via ``notify-send``
(org.freedesktop.Notifications). Notifications are id-reused: a stable
``notification_key`` maps to a numeric notification id (``-r``), so the same
logical event updates the existing desktop bubble instead of stacking
duplicates. The mapping is persisted in the database so restarts don't spam
the desktop.

All subprocess interaction goes through :func:`_run_notify_send`, which tests
mock; no real notify-send is invoked.
"""

import logging
import subprocess

from django.conf import settings

from .consts import NOTIFICATION_REUSE_WINDOW_MINUTES
from .models import NotificationState

logger = logging.getLogger(__name__)


class NotificationResult:
    def __init__(self, backend, delivered, notification_id=None, reason="",
                 action=None):
        self.backend = backend
        self.delivered = delivered
        self.notification_id = notification_id
        self.reason = reason
        self.action = action


class BaseNotificationBackend:
    name = "base"

    def notify(self, summary, body, notification_key=None, category=None,
               actions=None):
        raise NotImplementedError


class NullNotificationBackend(BaseNotificationBackend):
    """Notifications disabled: log only."""

    name = "none"

    def notify(self, summary, body, notification_key=None, category=None,
               actions=None):
        logger.info(
            "notification suppressed",
            extra={"backend": self.name, "summary": summary, "body": body},
        )
        return NotificationResult(self.name, delivered=False, reason="backend=none")


class DbusNotificationBackend(BaseNotificationBackend):
    """org.freedesktop.Notifications via notify-send on the session bus."""

    name = "dbus"

    def _run_notify_send(self, argv):
        """Seam for tests: actually invokes notify-send when unmocked."""
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def notify(self, summary, body, notification_key=None, category=None,
               actions=None):
        """Send a notification; ``actions`` is a list of (key, label) pairs.

        When the desktop environment supports notify-send -A, the process
        blocks until the notification is closed or an action is clicked and
        the clicked action key is returned in NotificationResult.action.
        """
        app_name = getattr(settings, "WATCHER_NOTIFY_APP_NAME", "vibe-watcher")
        argv = ["notify-send", "-a", app_name, "--expire-time", "10000", "--print-id"]

        reuse_id = self._lookup_reuse_id(app_name, notification_key)
        if reuse_id is not None:
            argv += ["-r", str(reuse_id)]
        if category:
            argv += ["-c", category]

        extra_args = getattr(settings, "WATCHER_NOTIFY_SEND_ARGS", "")
        if extra_args:
            argv += extra_args.split()

        for action_key, action_label in (actions or []):
            argv += ["-A", f"{action_key}={action_label}"]

        argv += [summary, body]

        try:
            proc = self._run_notify_send(argv)
        except FileNotFoundError:
            logger.warning(
                "notify-send not found; install libnotify-bin",
                extra={"backend": self.name},
            )
            return NotificationResult(
                self.name, delivered=False, reason="notify-send-missing"
            )
        except Exception as exc:
            logger.warning(
                "notification failed",
                extra={"backend": self.name, "error": str(exc)},
            )
            return NotificationResult(
                self.name, delivered=False, reason=f"error: {exc}"
            )

        if proc.returncode != 0:
            logger.warning(
                "notify-send failed",
                extra={
                    "backend": self.name,
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.strip(),
                },
            )
            return NotificationResult(
                self.name, delivered=False, reason=proc.stderr.strip()
            )

        notification_id = self._parse_notification_id(proc.stdout, fallback=reuse_id)
        if notification_key is not None and notification_id is not None:
            self._remember(app_name, notification_key, notification_id)

        return NotificationResult(
            self.name,
            delivered=True,
            notification_id=notification_id,
            action=self._parse_action(proc.stdout),
        )

    @staticmethod
    def _parse_notification_id(stdout, fallback=None):
        for line in stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return fallback

    @staticmethod
    def _parse_action(stdout):
        # notify-send -A prints the id on the first line and the chosen
        # action key (or "closed") on the last line.
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[-1].lower() != "closed":
            return lines[-1]
        return None

    @staticmethod
    def _lookup_reuse_id(app_name, notification_key):
        if notification_key is None:
            return None
        window_start_cutoff = None
        try:
            from django.utils import timezone

            cutoff = timezone.now() - timezone.timedelta(
                minutes=NOTIFICATION_REUSE_WINDOW_MINUTES
            )
            window_start_cutoff = cutoff
        except Exception:
            window_start_cutoff = None

        try:
            state = NotificationState.objects.get(
                app_name=app_name, notification_key=notification_key
            )
        except NotificationState.DoesNotExist:
            return None
        if window_start_cutoff is not None and state.notified_at < window_start_cutoff:
            return None
        return state.notification_id

    @staticmethod
    def _remember(app_name, notification_key, notification_id):
        NotificationState.objects.update_or_create(
            app_name=app_name,
            notification_key=notification_key,
            defaults={"notification_id": notification_id},
        )


def get_notification_backend(backend_name=None):
    name = backend_name or getattr(settings, "WATCHER_NOTIFY_BACKEND", "dbus")
    if name == "none":
        return NullNotificationBackend()
    if name == "dbus":
        return DbusNotificationBackend()
    raise ValueError(f"Unknown notification backend: {name!r}")
