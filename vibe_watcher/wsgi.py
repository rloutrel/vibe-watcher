"""WSGI config for vibe-watcher."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibe_watcher.settings")

application = get_wsgi_application()
