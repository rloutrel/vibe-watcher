from django.db import models

from .consts import (
    DEFAULT_POLL_INTERVAL,
    MAX_INTERVAL,
    NOTIFICATION_EXPIRY_DAYS,
    NOTIFICATION_REUSE_WINDOW_MINUTES,
)

REDACTED = "********"


def redact(value):
    if value is None:
        return None
    keep = 4
    if len(value) <= keep:
        return REDACTED
    return REDACTED + value[-keep:]


class Folder(models.Model):
    name = models.CharField(max_length=200)
    path = models.TextField()
    url = models.CharField(max_length=500, blank=True, default="")
    branch = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Branch to watch; empty means the currently checked-out branch.",
    )
    poll_interval = models.PositiveIntegerField(
        default=DEFAULT_POLL_INTERVAL,
        help_text="Seconds between polls for this folder.",
    )
    auto_pull = models.BooleanField(
        default=False,
        help_text="Run git pull --ff-only automatically when the remote changes.",
    )
    post_pull_command = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Shell command executed in the folder after a successful pull "
            "(e.g. docker compose up --build -d)."
        ),
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.poll_interval < 1:
            raise ValidationError({"poll_interval": "Must be at least 1 second."})
        if self.poll_interval > MAX_INTERVAL:
            raise ValidationError(
                {"poll_interval": f"Must be at most {MAX_INTERVAL} seconds."}
            )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "url": self.url,
            "branch": self.branch,
            "poll_interval": self.poll_interval,
            "auto_pull": self.auto_pull,
            "post_pull_command": self.post_pull_command,
            "enabled": self.enabled,
        }


class Snapshot(models.Model):
    """A recorded observation of a folder's git state."""

    folder = models.ForeignKey(
        Folder, on_delete=models.CASCADE, related_name="snapshots"
    )
    head = models.CharField(max_length=64, blank=True, default="")
    branch = models.CharField(max_length=200, blank=True, default="")
    upstream = models.CharField(max_length=300, blank=True, default="")
    state = models.CharField(max_length=32, default="unknown")
    commits_behind = models.PositiveIntegerField(default=0)
    commits_ahead = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        get_latest_by = "created_at"

    def __str__(self):
        return f"{self.folder.name} @ {self.head[:8]} ({self.created_at:%Y-%m-%d %H:%M})"


class WatcherEvent(models.Model):
    """Structured event visible in the web UI (recent activity feed)."""

    KIND_CHOICES = [
        ("scan", "scan"),
        ("change", "change"),
        ("error", "error"),
        ("notification", "notification"),
    ]

    folder = models.ForeignKey(
        Folder, on_delete=models.SET_NULL, related_name="events", null=True, blank=True
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="scan")
    message = models.TextField(blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.kind}] {self.message} ({self.created_at:%Y-%m-%d %H:%M})"


class WatcherStatus(models.Model):
    """Singleton-ish row holding the watcher heartbeat and last run info."""

    heartbeat = models.DateTimeField(null=True, blank=True)
    last_run = models.DateTimeField(null=True, blank=True)
    last_pass = models.DateTimeField(null=True, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    scanned_folders = models.PositiveIntegerField(default=0)
    iteration = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return "watcher status"

    def to_dict(self):
        return {
            "heartbeat": self.heartbeat,
            "last_run": self.last_run,
            "last_pass": self.last_pass,
            "pid": self.pid,
            "last_error": self.last_error,
            "scanned_folders": self.scanned_folders,
            "iteration": self.iteration,
        }


class NotifiedCommit(models.Model):
    """Tracks which notification ids were already used to avoid duplicates."""

    folder = models.ForeignKey(
        Folder, on_delete=models.CASCADE, related_name="notified_commits"
    )
    commit_hash = models.CharField(max_length=64)
    notified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["folder", "commit_hash"], name="uniq_notified_per_folder"
            )
        ]
        indexes = [
            models.Index(
                models.F("notified_at"),
                name="idx_notified_notified_at",
            )
        ]

    def __str__(self):
        return f"{self.folder.name}:{self.commit_hash[:8]}"

    @classmethod
    def prune(cls, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        cutoff = now - timezone.timedelta(days=NOTIFICATION_EXPIRY_DAYS)
        return cls.objects.filter(notified_at__lt=cutoff).delete()[0]


class NotificationState(models.Model):
    """Tracks D-Bus notification id reuse per (app_name, event_key).

    ``notification_key`` is a stable id for a logical notification, e.g.
    ``f<folder_id>:<commit>``. ``notification_id`` is the numeric id we pass
    to notify-send via -r so the desktop reuses/updates the same bubble.
    """

    app_name = models.CharField(max_length=200)
    notification_key = models.CharField(max_length=300)
    notification_id = models.PositiveIntegerField()
    notified_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["app_name", "notification_key"], name="uniq_notification_key"
            )
        ]

    def __str__(self):
        return f"{self.app_name}:{self.notification_key} -> {self.notification_id}"


class AppSetting(models.Model):
    """Simple key/value store for service configuration (e.g. git token)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=""):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

    @classmethod
    def set(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": value})
