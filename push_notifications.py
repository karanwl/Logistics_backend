from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Iterable

import requests
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db import SessionLocal
from db_models import PushToken

logger = logging.getLogger(__name__)

_TOKEN_LOCK = threading.Lock()
_TOKEN_REGISTRY: dict[str, set[str]] = {}


def _normalize_identity(value: str | None) -> str:
    return str(value or "").strip().lower()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _token_ttl_days() -> int:
    return max(1, int(settings.PUSH_TOKEN_TTL_DAYS or 120))


def _expires_at(now: datetime | None = None) -> datetime:
    now_value = now or _now_utc()
    return now_value + timedelta(days=_token_ttl_days())


def _cleanup_expired_tokens(session, now: datetime | None = None) -> None:
    now_value = now or _now_utc()
    session.execute(
        delete(PushToken).where(
            PushToken.expires_at.is_not(None),
            PushToken.expires_at <= now_value,
        )
    )


def _register_token_in_memory(identity: str, token: str) -> int:
    with _TOKEN_LOCK:
        tokens = _TOKEN_REGISTRY.setdefault(identity, set())
        tokens.add(token)
        return len(tokens)


def _unregister_token_in_memory(identity: str, token: str | None = None) -> int:
    tok = str(token or "").strip()
    with _TOKEN_LOCK:
        current = _TOKEN_REGISTRY.get(identity)
        if not current:
            return 0
        if tok:
            current.discard(tok)
        else:
            current.clear()
        if not current:
            _TOKEN_REGISTRY.pop(identity, None)
            return 0
        return len(current)


def _tokens_for_identities_in_memory(identities: Iterable[str]) -> list[str]:
    out: set[str] = set()
    with _TOKEN_LOCK:
        for ident in identities:
            for tok in _TOKEN_REGISTRY.get(ident, set()):
                if tok:
                    out.add(tok)
    return sorted(out)


def register_push_token(identity: str | None, token: str | None) -> int:
    ident = _normalize_identity(identity)
    tok = str(token or "").strip()
    if not ident or not tok:
        return 0
    now = _now_utc()
    try:
        with SessionLocal() as session:
            _cleanup_expired_tokens(session, now=now)
            row = session.get(PushToken, {"identity": ident, "token": tok})
            if row:
                row.last_seen_at = now
                row.updated_at = now
                row.expires_at = _expires_at(now)
            else:
                session.add(
                    PushToken(
                        identity=ident,
                        token=tok,
                        created_at=now,
                        updated_at=now,
                        last_seen_at=now,
                        expires_at=_expires_at(now),
                    )
                )
            count = session.execute(
                select(func.count())
                .select_from(PushToken)
                .where(
                    PushToken.identity == ident,
                    or_(PushToken.expires_at.is_(None), PushToken.expires_at > now),
                )
            ).scalar_one()
            session.commit()
            return int(count or 0)
    except SQLAlchemyError:
        logger.warning("Falling back to in-memory push token register.", exc_info=True)
        return _register_token_in_memory(ident, tok)


def unregister_push_token(identity: str | None, token: str | None = None) -> int:
    ident = _normalize_identity(identity)
    tok = str(token or "").strip()
    if not ident:
        return 0
    now = _now_utc()
    try:
        with SessionLocal() as session:
            _cleanup_expired_tokens(session, now=now)
            if tok:
                session.execute(
                    delete(PushToken).where(
                        PushToken.identity == ident,
                        PushToken.token == tok,
                    )
                )
            else:
                session.execute(delete(PushToken).where(PushToken.identity == ident))
            count = session.execute(
                select(func.count())
                .select_from(PushToken)
                .where(
                    PushToken.identity == ident,
                    or_(PushToken.expires_at.is_(None), PushToken.expires_at > now),
                )
            ).scalar_one()
            session.commit()
            return int(count or 0)
    except SQLAlchemyError:
        logger.warning("Falling back to in-memory push token unregister.", exc_info=True)
        return _unregister_token_in_memory(ident, tok or None)


def tokens_for_identities(identities: Iterable[str | None]) -> list[str]:
    normalized_identities = sorted(
        {
            _normalize_identity(identity)
            for identity in identities
            if _normalize_identity(identity)
        }
    )
    if not normalized_identities:
        return []
    now = _now_utc()
    try:
        with SessionLocal() as session:
            _cleanup_expired_tokens(session, now=now)
            rows = session.execute(
                select(PushToken.token)
                .where(
                    PushToken.identity.in_(normalized_identities),
                    or_(PushToken.expires_at.is_(None), PushToken.expires_at > now),
                )
                .distinct()
            ).all()
            session.commit()
            tokens = sorted({str(row[0]).strip() for row in rows if row and str(row[0]).strip()})
            return tokens
    except SQLAlchemyError:
        logger.warning("Falling back to in-memory push token lookup.", exc_info=True)
        return _tokens_for_identities_in_memory(normalized_identities)


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
