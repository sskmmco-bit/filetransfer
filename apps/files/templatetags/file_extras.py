"""Template helpers for rendering file rows (type icon + access badge)."""
from __future__ import annotations

from django import template

register = template.Library()

# Extension -> (css class, short label). Label falls back to the uppercased ext.
_DOC = {"pdf", "doc", "docx", "txt", "rtf", "odt", "md"}
_SHEET = {"xls", "xlsx", "csv", "ods"}
_IMG = {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "bmp", "heic", "tiff"}
_VID = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
_AUD = {"mp3", "wav", "ogg", "flac", "m4a", "aac"}
_ZIP = {"zip", "rar", "7z", "tar", "gz", "bz2"}
_CODE = {"py", "js", "ts", "json", "html", "css", "java", "go", "rb", "sh", "sql", "xml", "yml", "yaml"}


def _ext(name: str) -> str:
    name = name or ""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


@register.filter
def ftype(name: str) -> str:
    """CSS modifier class for the type-icon chip."""
    ext = _ext(name)
    if ext in _DOC or ext in _SHEET:
        return "doc"
    if ext in _IMG:
        return "img"
    if ext in _VID or ext in _AUD:
        return "vid"
    if ext in _ZIP:
        return "zip"
    if ext in _CODE:
        return "code"
    return "file"


@register.filter
def fext(name: str) -> str:
    """Short label shown inside the icon chip (e.g. PDF, PNG, FILE)."""
    ext = _ext(name)
    return ext.upper()[:4] if ext else "FILE"


@register.filter
def previewable(stored_file) -> bool:
    """True when the browser can render this file inline (image/pdf/video/audio/text)."""
    from apps.files.views import preview_kind

    if getattr(stored_file, "status", "") != "active":
        return False
    return preview_kind(
        getattr(stored_file, "content_type", "") or "",
        getattr(stored_file, "original_filename", "") or "",
    ) != "none"


@register.filter
def faccess(stored_file) -> str:
    """Access badge for an owner's file: 'pub' | 'team' | 'priv'.

    Maps directly to backend state — public link, shared with people/groups,
    or private (owner only). Avoid extra queries by preferring annotated counts.
    """
    if getattr(stored_file, "is_public", False):
        return "pub"
    n_assign = getattr(stored_file, "n_assign", None)
    n_group = getattr(stored_file, "n_group", None)
    if n_assign is None or n_group is None:  # not annotated — fall back to queries
        shared = stored_file.assignments.exists() or stored_file.groups.exists()
    else:
        shared = bool(n_assign or n_group)
    if shared:
        return "team"
    return "priv"
