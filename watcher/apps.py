from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


class WatcherConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "watcher"

    def ready(self):
        from .models import Folder
        from . import persist

        @receiver(post_save, sender=Folder, dispatch_uid="watcher.persist.save")
        @receiver(post_delete, sender=Folder, dispatch_uid="watcher.persist.delete")
        def persist_folder_config(sender, **kwargs):
            persist.save_config()
