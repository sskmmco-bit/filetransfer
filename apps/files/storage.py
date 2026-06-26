"""Low-level object operations backed by the local filesystem (§3, §5.5).

This deployment stores file blobs on the app host's local disk — there is no
MinIO/S3. Keys are relative paths under ``settings.FILE_STORAGE_ROOT`` and keep
the same layout the rest of the app already builds:

  temp:      temp/{uuid}/{filename}
  final:     files/{year}/{month}/{uuid}-{name}
  thumbnail: thumbnails/{uuid}.jpg

The public function names/signatures mirror the old S3 wrapper so callers are
unchanged, with two differences: there are no presigned URLs (delivery is done
by ``serve()`` below — nginx ``X-Accel-Redirect`` in production, a Django
``FileResponse`` in dev), and the chunked "multipart" upload is implemented by
appending parts to a single temp file (parts arrive strictly in order).
"""
from __future__ import annotations

import os
import shutil
from urllib.parse import quote

from django.conf import settings


# ----- path safety -------------------------------------------------------
def _root() -> str:
    return str(settings.FILE_STORAGE_ROOT)


def _safe_path(key: str) -> str:
    """Resolve a storage key to an absolute path under the storage root.

    Refuses any key that would escape the root (``..``/absolute paths), so a
    crafted key can never read or clobber a file outside the blob store.
    """
    root = os.path.realpath(_root())
    target = os.path.realpath(os.path.join(root, key))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"Unsafe storage key: {key!r}")
    return target


def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


# ----- direct (single-shot) transfer -------------------------------------
def put_object(key: str, fileobj, content_type: str = "application/octet-stream") -> None:
    """Write ``fileobj`` (a Django UploadedFile, BytesIO, etc.) to ``key``."""
    path = _safe_path(key)
    _ensure_parent(path)
    if hasattr(fileobj, "seek"):
        try:
            fileobj.seek(0)
        except (OSError, ValueError):
            pass
    with open(path, "wb") as dst:
        shutil.copyfileobj(fileobj, dst, length=8 * 1024 * 1024)


# ----- "multipart" (chunked) transfer ------------------------------------
# On disk there is no real multipart upload: parts arrive in order (the view
# uses session.next_part_number()) and are appended to a single temp file. The
# returned upload_id is just a non-empty marker the session stores.
def create_multipart(key: str, content_type: str = "application/octet-stream") -> str:
    path = _safe_path(key)
    _ensure_parent(path)
    # Truncate/create the temp file so a re-init starts clean.
    open(path, "wb").close()
    return key  # truthy marker; abort_multipart only needs the key


def upload_part(key: str, upload_id: str, part_number: int, data: bytes,
                offset: int | None = None) -> str:
    """Append one part's bytes to the temp file; return a part marker.

    ``offset`` (the bytes already committed for this upload) makes the write
    crash-/resume-safe: we seek to the known-good offset and truncate any
    partial tail from an interrupted earlier attempt before writing. When the
    caller doesn't pass it, we simply append.
    """
    path = _safe_path(key)
    _ensure_parent(path)
    if offset is None:
        with open(path, "ab") as f:
            f.write(data)
    else:
        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode) as f:
            f.seek(offset)
            f.write(data)
            f.truncate()
    return f'"{part_number}"'


def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> None:
    """No-op: the temp file was assembled in place by the appended parts."""
    return None


def abort_multipart(key: str, upload_id: str) -> None:
    delete_object(key)


# ----- object lifecycle --------------------------------------------------
def copy_object(src_key: str, dst_key: str) -> None:
    src = _safe_path(src_key)
    dst = _safe_path(dst_key)
    _ensure_parent(dst)
    shutil.copy2(src, dst)


def delete_object(key: str) -> None:
    if not key:
        return
    try:
        os.remove(_safe_path(key))
    except (FileNotFoundError, ValueError):
        pass
    except OSError:  # best-effort cleanup
        pass


def head_object(key: str) -> dict:
    st = os.stat(_safe_path(key))
    return {"ContentLength": st.st_size}


def object_exists(key: str) -> bool:
    """True if a blob is present at ``key``."""
    if not key:
        return False
    try:
        return os.path.isfile(_safe_path(key))
    except ValueError:
        return False


def get_object_body(key: str):
    """Open a blob for streaming reads (has .read() and .close())."""
    return open(_safe_path(key), "rb")


# ----- delivery ----------------------------------------------------------
def _effective_content_type(content_type: str, download_name: str) -> str:
    """Best content type for delivery.

    Uses the caller's content_type when it is specific; otherwise (empty or the
    generic application/octet-stream) guesses from the filename so browsers can
    render previews inline even when magic-byte sniffing was unavailable.
    """
    if content_type and content_type != "application/octet-stream":
        return content_type
    import mimetypes

    guessed, _ = mimetypes.guess_type(download_name)
    return guessed or content_type or "application/octet-stream"


def _content_disposition(download_name: str, inline: bool) -> str:
    """RFC 6266 Content-Disposition with an ASCII fallback + UTF-8 filename*."""
    disposition = "inline" if inline else "attachment"
    ascii_name = download_name.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_name = ascii_name.replace('"', "")
    encoded = quote(download_name, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def serve(key: str, *, download_name: str, inline: bool = False, content_type: str = ""):
    """Return a Django response delivering the blob at ``key`` (§5.4, §5.10).

    Replaces the old presigned-URL redirect. In production
    (``FILE_STORAGE_USE_X_ACCEL``) we hand nginx an ``X-Accel-Redirect`` to an
    internal location so it serves the bytes directly (zero-copy); in dev we
    stream the file through Django with ``FileResponse``. Callers must do their
    own authorization first — every byte served passes through app logic.
    """
    from django.http import FileResponse, Http404, HttpResponse

    disposition = _content_disposition(download_name, inline)
    ctype = _effective_content_type(content_type, download_name)

    if getattr(settings, "FILE_STORAGE_USE_X_ACCEL", False):
        if not object_exists(key):
            raise Http404("File not available.")
        resp = HttpResponse()
        # nginx decodes this URI against the internal location's alias.
        resp["X-Accel-Redirect"] = settings.FILE_STORAGE_X_ACCEL_PREFIX + quote(key, safe="/")
        resp["Content-Disposition"] = disposition
        resp["Content-Type"] = ctype
    else:
        path = _safe_path(key)
        if not os.path.isfile(path):
            raise Http404("File not available.")
        resp = FileResponse(open(path, "rb"))
        resp["Content-Disposition"] = disposition
        resp["Content-Type"] = ctype

    # Allow our own pages to embed inline previews in an <iframe> (PDF/text).
    # Django's default X-Frame-Options: DENY would otherwise block the frame
    # (the browser reports it as "refused to connect"). Same-origin only.
    resp["X-Frame-Options"] = "SAMEORIGIN"
    return resp
