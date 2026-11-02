"""Keyset pagination on ``(created_at DESC, id DESC)``.

Why keyset and not OFFSET: with OFFSET, inserting a row between two requests shifts every
later page, so a client sees a row twice or misses one, and the database still reads and
throws away all the skipped rows. A keyset cursor says "rows strictly after this
``(created_at, id)``", which is stable under inserts and is served straight from the index.

``id`` is in the key because ``created_at`` alone can tie (two uploads in the same
microsecond, or a bulk import); the tuple is unique, so the cursor never skips or repeats.

The cursor is opaque to clients: base64 of ``<created_at isoformat>|<uuid>``.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import HTTPException, Query, status

from app.config import get_settings


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        try:
            padded = token + "=" * (-len(token) % 4)
            ts, _, cid = base64.urlsafe_b64decode(padded).decode().partition("|")
            return cls(datetime.fromisoformat(ts), uuid.UUID(cid))
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Malformed cursor."
            ) from exc


@dataclass(frozen=True)
class Page:
    limit: int
    cursor: Cursor | None


def page_params(
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from `next_cursor`.")] = None,
) -> Page:
    settings = get_settings()
    size = min(limit or settings.page_size_default, settings.page_size_max)
    return Page(limit=size, cursor=Cursor.decode(cursor) if cursor else None)
