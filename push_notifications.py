from __future__ import annotations

import logging
import threading
from typing import Iterable

import requests

from config import settings

logger = logging.getLogger(__name__)

_TOKEN_LOCK = threading.Lock()
_TOKEN_REGISTRY: dict[str, set[str]] = {}


def _normalize_identity(value: str | None) -> str:
    return str(value or "").strip().lower()


def register_push_token(identity: str | None, token: str | None) -> int:
    ident = _normalize_identity(identity)
    tok = str(token or "").strip()
    if not ident or not tok:
        return 0
    with _TOKEN_LOCK:
        tokens = _TOKEN_REGISTRY.setdefault(ident, set())
        tokens.add(tok)
        return len(tokens)


def unregister_push_token(identity: str | None, token: str | None = None) -> int:
    ident = _normalize_identity(identity)
    tok = str(token or "").strip()
    if not ident:
        return 0
    with _TOKEN_LOCK:
        current = _TOKEN_REGISTRY.get(ident)
        if not current:
            return 0
        if tok:
            current.discard(tok)
        else:
            current.clear()
        if not current:
            _TOKEN_REGISTRY.pop(ident, None)
            return 0
        return len(current)


def tokens_for_identities(identities: Iterable[str | None]) -> list[str]:
    out: set[str] = set()
    with _TOKEN_LOCK:
        for ident in identities:
            key = _normalize_identity(ident)
            if not key:
                continue
            for tok in _TOKEN_REGISTRY.get(key, set()):
                if tok:
                    out.add(tok)
    return sorted(out)


def send_push_notification(
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict | None = None,
) -> dict:
    clean_tokens = [str(t).strip() for t in (tokens or []) if str(t).strip()]
    if not clean_tokens:
        return {"ok": False, "reason": "no_tokens", "sent": 0}
    if not settings.FCM_SERVER_KEY:
        return {"ok": False, "reason": "missing_fcm_server_key", "sent": 0}

    payload = {
        "registration_ids": clean_tokens,
        "priority": "high",
        "notification": {
            "title": title,
            "body": body,
        },
        "data": data or {},
    }
    headers = {
        "Authorization": f"key={settings.FCM_SERVER_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            settings.FCM_SEND_URL,
            json=payload,
            headers=headers,
            timeout=settings.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        parsed = response.json() if response.content else {}
        success = int(parsed.get("success") or 0) if isinstance(parsed, dict) else 0
        failure = int(parsed.get("failure") or 0) if isinstance(parsed, dict) else 0
        return {
            "ok": True,
            "sent": success,
            "failed": failure,
            "requested": len(clean_tokens),
        }
    except Exception as exc:
        logger.warning("Push send failed: %s", exc, exc_info=True)
        return {"ok": False, "reason": str(exc), "sent": 0}
