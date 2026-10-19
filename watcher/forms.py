from django import forms

from .consts import DEFAULT_POLL_INTERVAL, MAX_INTERVAL
from .git import expand_path
from .models import Folder


class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = [
            "name", "path", "url", "branch", "poll_interval",
            "auto_pull", "post_pull_command", "git_token", "enabled",
        ]
        widgets = {
            "path": forms.TextInput(attrs={"placeholder": "~/code/my-repo or /home/me/code/my-repo"}),
            "url": forms.TextInput(attrs={"placeholder": "https://github.com/me/my-repo (optional, autodetected)"}),
            "post_pull_command": forms.TextInput(
                attrs={"placeholder": "docker compose up --build -d (optional)"}
            ),
            "git_token": forms.PasswordInput(
                render_value=False,
                attrs={"placeholder": "Leave empty to use the global token"},
            ),
            "branch": forms.Select(
                attrs={
                    "id": "id_branch",
                    "data-placeholder": "Leave empty for the checked-out branch",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Quick add: the name is autodetected from the repo directory if blank.
        self.fields["name"].required = False
        self.fields["name"].widget.attrs["placeholder"] = "Leave empty to autodetect from the path"

    def clean_path(self):
        raw = self.cleaned_data.get("path", "").strip()
        if not raw:
            raise forms.ValidationError("Path is required.")
        return expand_path(raw)

    def clean_poll_interval(self):
        value = self.cleaned_data.get("poll_interval")
        if value is None:
            return DEFAULT_POLL_INTERVAL
        if value < 1 or value > MAX_INTERVAL:
            raise forms.ValidationError(
                f"Poll interval must be between 1 and {MAX_INTERVAL} seconds."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        # Quick add: derive the name from the repo directory when left blank.
        if not cleaned.get("name"):
            path = cleaned.get("path", "").rstrip("/")
            cleaned["name"] = path.split("/")[-1] if path else ""
            if not cleaned["name"]:
                self.add_error("name", "Name is required (could not derive it from the path).")
        return cleaned
