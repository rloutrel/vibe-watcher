from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .consts import DEFAULT_POLL_INTERVAL
from .forms import FolderForm
from .models import Folder, Snapshot, WatcherEvent, WatcherStatus


def watcher_status():
    """Public helper the UI uses to flag a stale/stopped watcher."""
    status = WatcherStatus.objects.first()
    if status is None or status.heartbeat is None:
        return {"state": "never-run", "stale": True, "status": status}
    stale_after = getattr(settings, "WATCHER_HEARTBEAT_STALE_AFTER", 180)
    age = (timezone.now() - status.heartbeat).total_seconds()
    return {
        "state": "ok" if age <= stale_after else "stale",
        "stale": age > stale_after,
        "age_seconds": int(age),
        "status": status,
    }


@login_required
def index(request):
    folders = list(Folder.objects.order_by("name"))
    latest_by_folder = {}
    for folder in folders:
        latest_by_folder[folder.id] = Snapshot.objects.filter(folder=folder).first()
    rows = [(folder, latest_by_folder.get(folder.id)) for folder in folders]
    events = WatcherEvent.objects.select_related("folder")[:50]
    return render(
        request,
        "watcher/index.html",
        {
            "rows": rows,
            "events": events,
            "watcher": watcher_status(),
        },
    )


@login_required
def folder_create(request):
    if request.method == "POST":
        form = FolderForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("watcher:index")
    else:
        form = FolderForm(initial={"poll_interval": DEFAULT_POLL_INTERVAL})
    return render(request, "watcher/folder_form.html", {"form": form})


@login_required
def folder_update(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    if request.method == "POST":
        form = FolderForm(request.POST, instance=folder)
        if form.is_valid():
            form.save()
            return redirect("watcher:index")
    else:
        form = FolderForm(instance=folder)
    return render(
        request,
        "watcher/folder_form.html",
        {"form": form, "folder": folder},
    )


@login_required
def folder_delete(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    if request.method == "POST":
        folder.delete()
    return redirect("watcher:index")
