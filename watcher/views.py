import json
import os

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
from .git import (NO_REMOTE_MESSAGE, GitError, GitRepo, detect_repo,
                  expand_path, find_repos, list_dir_entries)
from .models import Folder, Snapshot, WatcherEvent, WatcherStatus, redact
from .scanner import record_event


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


def _index_context(add_form=None, edit_folder=None, edit_form=None):
    """Context for the main page: watcher state, folder rows, events,
    and the inlined folder/token configuration (accordion)."""
    from .actions import docker_running
    from .git import code_version

    folders = list(Folder.objects.order_by("name"))
    latest_by_folder = {}
    stack_down = {}
    for folder in folders:
        latest_by_folder[folder.id] = Snapshot.objects.filter(folder=folder).first()
        # Show the start button when the project has a docker compose
        # post-pull command but its stack isn't running.
        if folder.enabled and "docker compose" in (folder.post_pull_command or ""):
            stack_down[folder.id] = docker_running(folder) is False
        else:
            stack_down[folder.id] = False
    rows = [
        (
            folder,
            latest_by_folder.get(folder.id),
            stack_down[folder.id],
            edit_form if edit_folder is not None and folder.pk == edit_folder.pk else None,
            FolderForm(instance=folder),
        )
        for folder in folders
    ]
    token, token_status = _token_masked()
    if add_form is None:
        add_form = FolderForm(initial={"poll_interval": DEFAULT_POLL_INTERVAL})
    return {
        "rows": rows,
        "events": WatcherEvent.objects.select_related("folder")[:50],
        "watcher": watcher_status(),
        "code_version": code_version(),
        "token_hint": "ghp_••••••••••••••••••••" if token else "",
        "token_status": token_status,
        "folders": folders,
        "add_form": add_form,
    }


@login_required
def index(request):
    return render(request, "watcher/index.html", _index_context())


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
    if not repo.is_repo():
        return {
            "branches": [],
            "has_remote": False,
            "no_remote_error": "",
            "error": "not a git repository",
        }
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
        add_form = form
    else:
        add_form = FolderForm(initial={"poll_interval": DEFAULT_POLL_INTERVAL})
    return render(request, "watcher/index.html", _index_context(add_form=add_form))


@login_required
def folder_update(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    if request.method == "POST":
        form = FolderForm(request.POST, instance=folder)
        if form.is_valid():
            folder = form.save(commit=False)
            if not form.cleaned_data.get("git_token"):
                # empty password field = keep the existing token
                folder.git_token = get_object_or_404(Folder, pk=pk).git_token
            folder.save()
            return redirect("watcher:index")
        edit_form = form
    else:
        edit_form = FolderForm(instance=folder)
    return render(
        request,
        "watcher/index.html",
        _index_context(edit_folder=folder, edit_form=edit_form),
    )


@login_required
def folder_delete(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    if request.method == "POST":
        folder.delete()
    return redirect("watcher:index")


BROWSE_ROOTS = ["~", "/"]


@login_required
@require_GET
def browse_path(request):
    """Directory picker backend: list subdirectories of a path.

    Accepts ?path= (defaults to the home dir). Handles ~ expansion and
    returns parent/children so the UI can navigate up and down. Only
    directories are listed.
    """
    import os

    raw = request.GET.get("path", "~")
    target = expand_path(raw) if raw.startswith("~") else raw
    dirs, error = list_dir_entries(target)
    if dirs is None and raw != "~":
        return JsonResponse({"error": error}, status=400)
    if dirs is None:
        return JsonResponse({"error": error}, status=400)

    parent = os.path.dirname(target.rstrip("/")) or "/"
    return JsonResponse(
        {
            "path": target,
            "parent": parent if parent != target else None,
            "dirs": dirs,
        }
    )


@login_required
@require_POST
def folder_toggle(request, pk):
    """Flip a folder's enabled flag (ON/OFF switch)."""
    folder = get_object_or_404(Folder, pk=pk)
    folder.enabled = not folder.enabled
    folder.save(update_fields=["enabled"])
    return JsonResponse({"id": folder.id, "enabled": folder.enabled})


@login_required
@require_POST
def folder_rename(request, pk):
    """Rename a folder in place (inline input in the card summary)."""
    folder = get_object_or_404(Folder, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "missing name"}, status=400)
    folder.name = name
    folder.save(update_fields=["name"])
    return JsonResponse({"id": folder.id, "name": folder.name})


@login_required
def folders_export(request):
    """Download the folder configuration as JSON (local backup)."""
    import json

    payload = {
        "app": "vibe-watcher",
        "version": 1,
        "exported_at": timezone.now().isoformat(),
        "folders": [f.to_dict() for f in Folder.objects.all()],
    }
    response = HttpResponse(
        json.dumps(payload, indent=2), content_type="application/json"
    )
    response["Content-Disposition"] = 'attachment; filename="vibe-watcher-config.json"'
    return response


@login_required
@require_POST
def folders_import(request):
    """Restore folders from a previously exported JSON config.

    Existing folders are matched by path and updated; new ones are created.
    Snapshots/events are untouched — only the configuration is recovered.
    """
    import json

    from .consts import DEFAULT_POLL_INTERVAL, MAX_INTERVAL

    uploaded = request.FILES.get("config")
    if uploaded is None:
        return JsonResponse({"error": "no config file provided"}, status=400)
    try:
        payload = json.loads(uploaded.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return JsonResponse({"error": f"invalid JSON: {exc}"}, status=400)

    entries = payload.get("folders")
    if not isinstance(entries, list):
        return JsonResponse(
            {"error": "invalid config: missing 'folders' list"}, status=400
        )

    created = updated = skipped = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            skipped += 1
            continue
        path = entry["path"]
        interval = entry.get("poll_interval") or DEFAULT_POLL_INTERVAL
        interval = max(1, min(int(interval), MAX_INTERVAL))
        folder, was_created = Folder.objects.update_or_create(
            path=path,
            defaults={
                "name": entry.get("name") or path.rstrip("/").split("/")[-1],
                "url": entry.get("url") or "",
                "branch": entry.get("branch") or "",
                "poll_interval": interval,
                "enabled": bool(entry.get("enabled", True)),
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return JsonResponse({"created": created, "updated": updated, "skipped": skipped})


@login_required
@require_POST
def folder_start_docker(request, pk):
    """Manually start the folder's docker stack (the play button)."""
    from .actions import start_docker

    folder = get_object_or_404(Folder, pk=pk)
    ok, output = start_docker(folder)
    record_event(
        "change" if ok else "error",
        f"{folder.name}: docker stack {'started' if ok else 'failed to start'} (manual)",
        folder=folder,
        detail={"output": output[-500:]},
    )
    return redirect("watcher:index")


@login_required
@require_POST
def folder_stop_docker(request, pk):
    """Manually stop the folder's docker stack."""
    from .actions import stop_docker

    folder = get_object_or_404(Folder, pk=pk)
    ok, output = stop_docker(folder)
    record_event(
        "change" if ok else "error",
        f"{folder.name}: docker stack {'stopped' if ok else 'failed to stop'} (manual)",
        folder=folder,
        detail={"output": output[-500:]},
    )
    return redirect("watcher:index")


@login_required
@require_POST
def folder_check_now(request, pk):
    """Scan one folder immediately and return the fresh snapshot."""
    from . import scanner
    from .notify import get_notification_backend

    folder = get_object_or_404(Folder, pk=pk)
    snapshot = scanner.scan_folder(
        folder, backend=get_notification_backend("none")
    )
    return JsonResponse(
        {
            "id": folder.id,
            "state": snapshot.state,
            "branch": snapshot.branch,
            "upstream": snapshot.upstream,
            "commits_behind": snapshot.commits_behind,
            "commits_ahead": snapshot.commits_ahead,
            "error": snapshot.error,
        }
    )


def _token_masked():
    from .models import AppSetting

    token = AppSetting.get("git_token")
    if not token:
        return None, "No token configured."
    return token, f"Configured: {redact(token)} (leave blank to keep)"


@login_required
def config_page(request):
    return redirect("watcher:index")


@login_required
@require_POST
def config_token(request):
    from .models import AppSetting

    token = (request.POST.get("token") or "").strip()
    if token:
        AppSetting.set("git_token", token)
        os.environ["VIBE_WATCHER_GIT_TOKEN"] = token
    return redirect("watcher:index")


@login_required
def config_token_delete(request):
    from .models import AppSetting

    AppSetting.objects.filter(key="git_token").delete()
    os.environ.pop("VIBE_WATCHER_GIT_TOKEN", None)
    return redirect("watcher:index")


@login_required
@require_GET
def find_projects(request):
    """Find all git repositories under a directory (recursive, bounded)."""
    raw = request.GET.get("path") or os.environ.get(
        "VIBE_WATCHER_PROJECTS_BASE"
    ) or getattr(settings, "WATCHER_PROJECTS_BASE", "~")
    base = expand_path(raw) if raw.startswith("~") else raw
    repos, error = find_repos(base)
    if repos is None:
        return JsonResponse({"error": error}, status=400)

    already = set(
        Folder.objects.values_list("path", flat=True)
    )
    projects = []
    for repo_path in repos:
        name = repo_path.rstrip("/").split("/")[-1]
        projects.append(
            {
                "path": repo_path,
                "name": name,
                "registered": repo_path in already,
            }
        )
    return JsonResponse({"base": base, "projects": projects, "error": error})
