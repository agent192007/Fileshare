from django import forms

from config.env import load_env

load_env()

class MultiUploadForm(forms.Form):
    files = forms.FileField(widget=forms.ClearableFileInput(attrs={'multiple': True}))
