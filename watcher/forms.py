from django import forms

from .consts import DEFAULT_POLL_INTERVAL, MAX_INTERVAL
from .models import Folder


class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ["name", "path", "url", "branch", "poll_interval", "enabled"]
        widgets = {
            "path": forms.TextInput(attrs={"placeholder": "/home/me/code/my-repo"}),
            "url": forms.TextInput(attrs={"placeholder": "https://github.com/me/my-repo (optional)"}),
            "branch": forms.TextInput(
                attrs={
                    "placeholder": "Leave empty for the checked-out branch",
                    "list": "branch-options",
                }
            ),
        }

    def clean_poll_interval(self):
        value = self.cleaned_data.get("poll_interval")
        if value is None:
            return DEFAULT_POLL_INTERVAL
        if value < 1 or value > MAX_INTERVAL:
            raise forms.ValidationError(
                f"Poll interval must be between 1 and {MAX_INTERVAL} seconds."
            )
        return value
