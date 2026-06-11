from django.contrib.auth.forms import AuthenticationForm
from django.forms import CharField, TextInput


class IdentifierAuthenticationForm(AuthenticationForm):
    """Login form whose first field accepts employee ID, email, or username."""

    username = CharField(
        label="Employee ID, email, or username",
        widget=TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )
