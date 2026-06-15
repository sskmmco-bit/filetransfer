"""Shared helpers for MMFileTransfer.

get_client_ip() is the single source of truth for a request's real client IP
(§3). Because nginx sits in front of Gunicorn, REMOTE_ADDR is the proxy, not the
client. We trust X-Forwarded-For ONLY when the direct peer is a configured
trusted proxy, so clients cannot spoof their address by sending the header.
Every audit / security record must use this helper.
"""
from __future__ import annotations

from django.conf import settings


def get_client_ip(request) -> str | None:
    """Return the best-effort real client IP, resistant to XFF spoofing."""
    remote_addr = request.META.get("REMOTE_ADDR")
    trusted = set(getattr(settings, "TRUSTED_PROXY_IPS", []) or [])
    xff = request.META.get("HTTP_X_FORWARDED_FOR")

    # Only honour X-Forwarded-For if the request actually came from a trusted
    # proxy; otherwise the header is attacker-controlled and ignored.
    if remote_addr in trusted and xff:
        # XFF is "client, proxy1, proxy2"; walk from the right and return the
        # first hop that is not itself a trusted proxy — that is the client.
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            if hop not in trusted:
                return hop
        if hops:
            return hops[0]

    return remote_addr


def mask_ip(ip: str | None) -> str:
    """Partially redact an IP for display in activity feeds."""
    if not ip:
        return ""
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.xxx.xxx"
    if ":" in ip:
        parts = ip.split(":")
        if len(parts) >= 2:
            return ":".join(parts[:2]) + ":xxxx"
    return ip
