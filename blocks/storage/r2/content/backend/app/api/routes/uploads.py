"""Presigned-upload endpoints backed by Cloudflare R2.

`POST /api/uploads/presign` returns a presigned PUT URL the client uploads to
directly. The backend never sees the bytes. On completion the client `POST`s
back to `/api/uploads/commit` with the key, which we record against the user.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.clerk import ClerkPrincipal, current_user
from app.core.r2 import presign_put, public_url

router = APIRouter()


class PresignRequest(BaseModel):
    filename: str = Field(..., max_length=255)
    content_type: str = Field(..., max_length=127)
    size_bytes: int = Field(..., ge=1, le=100 * 1024 * 1024)  # 100 MiB cap


class PresignResponse(BaseModel):
    upload_url: str
    key: str
    public_url: str | None


@router.post("/presign", response_model=PresignResponse)
def presign(
    body: PresignRequest,
    user: Annotated[ClerkPrincipal, Depends(current_user)],
) -> PresignResponse:
    """Allocate a key under the current user and return a presigned PUT URL."""
    if not body.filename.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Empty filename")
    safe_name = body.filename.replace("/", "_").replace("\\", "_")
    key = f"u/{user.sub}/{uuid.uuid4()}/{safe_name}"
    url = presign_put(key, content_type=body.content_type)
    try:
        pub = public_url(key)
    except RuntimeError:
        pub = None
    return PresignResponse(upload_url=url, key=key, public_url=pub)
