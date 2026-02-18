from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from app_errors import ConflictError
from config import settings
from db import SessionLocal
from db_models import IdempotencyKey

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_seconds() -> int:
    return max(60, int(settings.IDEMPOTENCY_TTL_SECONDS or 86400))


def _fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_key(value: str | None) -> str:
    return str(value or "").strip()


@dataclass
class IdempotencyReplay:
    status_code: int
    body: dict[str, Any]


@dataclass
class IdempotencyContext:
    key: str
    request_hash: str


class IdempotencyService:
    @staticmethod
    def begin(
        *,
        key: str | None,
        method: str,
        path: str,
        payload: Any,
    ) -> tuple[IdempotencyContext | None, IdempotencyReplay | None]:
        normalized_key = _normalize_key(key)
        if not normalized_key:
            return None, None

        request_hash = _fingerprint(payload)
        now = _now_utc()
        expires_at = now + timedelta(seconds=_ttl_seconds())

        try:
            with SessionLocal() as session:
                session.execute(
                    delete(IdempotencyKey).where(
                        IdempotencyKey.expires_at.is_not(None),
                        IdempotencyKey.expires_at <= now,
                    )
                )

                row = session.get(IdempotencyKey, normalized_key)
                if row:
                    if (
                        row.request_method != method
                        or row.request_path != path
                        or row.request_hash != request_hash
                    ):
                        raise ConflictError("Idempotency key reuse detected with different request payload.")
                    if row.response_body:
                        try:
                            body = json.loads(row.response_body)
                        except Exception:
                            body = {
                                "ok": False,
                                "error": {
                                    "message": "Stored idempotent response is unreadable.",
                                    "code": "INTERNAL_ERROR",
                                },
                            }
                        return None, IdempotencyReplay(status_code=int(row.status_code or 200), body=body)
                    raise ConflictError("A request with this idempotency key is already in progress.")

                session.add(
                    IdempotencyKey(
                        idempotency_key=normalized_key,
                        request_method=method,
                        request_path=path,
                        request_hash=request_hash,
                        created_at=now,
                        updated_at=now,
                        expires_at=expires_at,
                    )
                )
                session.commit()
                return IdempotencyContext(key=normalized_key, request_hash=request_hash), None
        except SQLAlchemyError as exc:
            logger.warning(
                "Idempotency store unavailable; continuing without idempotency key. key=%s method=%s path=%s",
                normalized_key,
                method,
                path,
                exc_info=exc,
            )
            return None, None

    @staticmethod
    def complete(context: IdempotencyContext | None, *, status_code: int, body: dict[str, Any]) -> None:
        if context is None:
            return
        now = _now_utc()
        try:
            with SessionLocal() as session:
                row = session.get(IdempotencyKey, context.key)
                if not row or row.request_hash != context.request_hash:
                    return
                row.status_code = int(status_code)
                row.response_body = json.dumps(body, separators=(",", ":"), default=str)
                row.completed_at = now
                row.updated_at = now
                session.commit()
        except SQLAlchemyError:
            logger.warning(
                "Idempotency completion failed; response will still be returned. key=%s",
                context.key,
                exc_info=True,
            )

    @staticmethod
    def abandon(context: IdempotencyContext | None) -> None:
        if context is None:
            return
        try:
            with SessionLocal() as session:
                session.execute(
                    delete(IdempotencyKey).where(
                        IdempotencyKey.idempotency_key == context.key,
                        IdempotencyKey.request_hash == context.request_hash,
                        IdempotencyKey.response_body.is_(None),
                    )
                )
                session.commit()
        except SQLAlchemyError:
            logger.warning(
                "Idempotency abandon failed; proceeding without cleanup. key=%s",
                context.key,
                exc_info=True,
            )
