import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from .consts import DEFAULT_POLL_INTERVAL
from .forms import FolderForm
from .git import NO_REMOTE_MESSAGE, GitError, GitRepo, detect_repo
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
@require_GET
def detect_folder(request):
    """Quick add: autodetect repo info from an entered path.

    Handles ``~`` expansion server-side, so a path like ``~/code/my-repo``
    resolves against the watcher user's home. Returns resolved path, repo
    name (directory basename), checked-out branch, remote URL, and branches.
    """
    raw = request.GET.get("path", "").strip()
    if not raw:
        return JsonResponse({"error": "missing path"}, status=400)
    return JsonResponse(detect_repo(raw))


def _branch_payload(repo):
    """Structured branch list for the picker.

    Branches existing both locally and on the remote are shown once (the
    local name). Branches with no remote-tracking counterpart are marked
    ``disabled`` — watching them cannot detect remote changes. When the repo
    has no remote at all, every entry is disabled and ``no_remote`` is set.
    """
    choices = repo.branch_choices()
    has_remote = repo.has_remote()
    payload = {
        "branches": [
            {
                "name": c["name"],
                "current": c["current"],
                "remote": c["remote"],
                "disabled": (not has_remote) or (not c["remote"]),
            }
            for c in choices
        ],
        "has_remote": has_remote,
        "no_remote_error": "" if has_remote else NO_REMOTE_MESSAGE,
    }
    return payload


@login_required
@require_GET
def folder_branches(request, pk):
    """JSON list of branches available in a saved folder's repo (git branch -a)."""
    folder = get_object_or_404(Folder, pk=pk)
    try:
        repo = GitRepo(folder.path)
        return JsonResponse(_branch_payload(repo))
    except GitError as exc:
        return JsonResponse({"error": str(exc)}, status=502)


@login_required
@require_GET
def branches_for_path(request):
    """JSON list of branches for an unsaved path (used by the Add form)."""
    path = request.GET.get("path", "").strip()
    if not path:
        return JsonResponse({"error": "missing path"}, status=400)
    try:
        repo = GitRepo(path)
        return JsonResponse(_branch_payload(repo))
    except GitError as exc:
        return JsonResponse({"error": str(exc)}, status=502)


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
