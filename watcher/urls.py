from django.urls import path

from . import views

app_name = "watcher"

urlpatterns = [
    path("", views.index, name="index"),
    path("folders/new/", views.folder_create, name="folder-create"),
    path("folders/<int:pk>/edit/", views.folder_update, name="folder-update"),
    path("folders/<int:pk>/delete/", views.folder_delete, name="folder-delete"),
]
