from django.contrib import admin

from .models import Folder, NotifiedCommit, NotificationState, Snapshot, WatcherEvent, WatcherStatus


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ("name", "path", "poll_interval", "enabled", "created_at")
    list_filter = ("enabled",)
    search_fields = ("name", "path")


@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = ("folder", "state", "commits_behind", "commits_ahead", "created_at")
    list_filter = ("state",)


@admin.register(WatcherEvent)
class WatcherEventAdmin(admin.ModelAdmin):
    list_display = ("kind", "folder", "message", "created_at")
    list_filter = ("kind",)


@admin.register(WatcherStatus)
class WatcherStatusAdmin(admin.ModelAdmin):
    list_display = ("heartbeat", "last_pass", "pid", "scanned_folders", "iteration")


@admin.register(NotifiedCommit)
class NotifiedCommitAdmin(admin.ModelAdmin):
    list_display = ("folder", "commit_hash", "notified_at")


@admin.register(NotificationState)
class NotificationStateAdmin(admin.ModelAdmin):
    list_display = ("app_name", "notification_key", "notification_id", "notified_at")
