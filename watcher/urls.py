from django.urls import path

from . import views

app_name = "watcher"

urlpatterns = [
    path("", views.index, name="index"),
    path("folders/new/", views.folder_create, name="folder-create"),
    path("folders/<int:pk>/edit/", views.folder_update, name="folder-update"),
    path("folders/<int:pk>/delete/", views.folder_delete, name="folder-delete"),
    path("folders/<int:pk>/branches/", views.folder_branches, name="folder-branches"),
    path("folders/branches/", views.branches_for_path, name="branches-for-path"),
    path("folders/detect/", views.detect_folder, name="folder-detect"),
    path("folders/<int:pk>/toggle/", views.folder_toggle, name="folder-toggle"),
    path("folders/export/", views.folders_export, name="folders-export"),
    path("folders/import/", views.folders_import, name="folders-import"),
]
