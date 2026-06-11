from django import forms


class EmailForm(forms.Form):
    email = forms.EmailField(label="Your email address")


class CodeForm(forms.Form):
    code = forms.CharField(label="Verification code", max_length=6, min_length=6)
