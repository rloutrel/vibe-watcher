"""Management command: ``python manage.py watch_folders``.

Runs the watcher as a long-lived foreground process — the ExecStart target of
the systemd unit. Each enabled folder is polled on its own interval inside
this single process (no Celery, no scheduler thread).
"""

import logging

from django.core.management.base import BaseCommand

from watcher import persist, scanner

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Poll enabled folders for upstream git changes and notify (long-running)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single scan pass over all enabled folders and exit.",
        )
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=None,
            help="Exit after N loop iterations (mainly for testing).",
        )
        parser.add_argument(
            "--notify-backend",
            choices=["dbus", "none"],
            default=None,
            help="Override the WATCHER_NOTIFY_BACKEND setting.",
        )

    def handle(self, *args, **options):
        from watcher.models import AppSetting

        token = AppSetting.get("git_token")
        if token:
            import os

            os.environ["VIBE_WATCHER_GIT_TOKEN"] = token
            self.stdout.write("Git token loaded from configuration.")

        restored = persist.restore_if_needed()
        if restored:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Recovered {restored} folder(s) from the local config file."
                )
            )
        backend = None
        if options["notify_backend"]:
            from watcher.notify import get_notification_backend

            backend = get_notification_backend(options["notify_backend"])

        if options["once"]:
            snapshots, errors = scanner.scan_all_once(backend=backend)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Scanned {snapshots + errors} folder(s), {errors} error(s)."
                )
            )
            return

        self.stdout.write(
            "watch_folders: starting loop (Ctrl+C to stop; systemd Restart=on-failure handles crashes)"
        )
        try:
            scanner.run_loop(backend=backend, max_iterations=options["max_iterations"])
        except KeyboardInterrupt:
            self.stdout.write("watch_folders: interrupted, shutting down cleanly.")
