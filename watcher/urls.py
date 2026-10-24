from django.urls import path

from . import views

app_name = "watcher"

urlpatterns = [
    path("", views.index, name="index"),
    path("config/", views.config_page, name="config-page"),
    path("config/token/", views.config_token, name="config-token"),
    path("config/token/delete/", views.config_token_delete, name="config-token-delete"),
    path("folders/new/", views.folder_create, name="folder-create"),
    path("folders/<int:pk>/edit/", views.folder_update, name="folder-update"),
    path("folders/<int:pk>/delete/", views.folder_delete, name="folder-delete"),
    path("folders/<int:pk>/branches/", views.folder_branches, name="folder-branches"),
    path("folders/branches/", views.branches_for_path, name="branches-for-path"),
    path("folders/detect/", views.detect_folder, name="folder-detect"),
    path("folders/browse/", views.browse_path, name="folder-browse"),
    path("folders/find/", views.find_projects, name="folder-find"),
    path("folders/<int:pk>/toggle/", views.folder_toggle, name="folder-toggle"),
    path("folders/<int:pk>/rename/", views.folder_rename, name="folder-rename"),
    path("folders/<int:pk>/check/", views.folder_check_now, name="folder-check-now"),
    path("folders/<int:pk>/start/", views.folder_start_docker, name="folder-start-docker"),
    path("folders/<int:pk>/stop/", views.folder_stop_docker, name="folder-stop-docker"),
    path("events/clear/", views.events_clear, name="events-clear"),
    path("folders/export/", views.folders_export, name="folders-export"),
    path("folders/import/", views.folders_import, name="folders-import"),
]
