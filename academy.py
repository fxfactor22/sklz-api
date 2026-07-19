"""SKLZ Academy — live sessions, recordings, and events.

Admin (you) creates content; members watch. Access model:
  - each item has access: "preview" (everyone) or "full" (subscribers)
  - non-subscribers see preview items + locked cards for full items
  - live items embed an external stream URL (YouTube Live / Zoom)
  - recordings embed a video URL (YouTube / Vimeo / hosted)

Endpoints:
  GET  /api/academy/content            list (server marks what caller can watch)
  POST /api/academy/item      [admin]  create session/recording/event
  PATCH/DELETE /api/academy/item/{id} [admin]
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/academy", tags=["academy"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_admin(user) -> bool:
    return (getattr(user, "email", "") or "").lower() in _admin_emails()


def _has_full_access(sb: Client, user) -> bool:
    if _is_admin(user):
        return True
    try:
        r = (sb.table("subscriptions").select("plan,active")
             .eq("user_id", str(user.id)).execute()).data or []
        return any(row.get("active") for row in r)
    except Exception:
        return False


class ItemIn(BaseModel):
    kind: str = "recording"          # live | recording | event
    title: str
    description: str = ""
    access: str = "full"             # preview | full
    video_url: str = ""              # embed URL (recording) or stream (live)
    thumbnail: str = ""
    starts_at: str | None = None     # for live/event scheduling
    duration_min: int | None = None
    tags: list[str] = Field(default_factory=list)
    published: bool = True


class ItemPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    access: str | None = None
    video_url: str | None = None
    thumbnail: str | None = None
    starts_at: str | None = None
    duration_min: int | None = None
    published: bool | None = None
    tags: list[str] | None = None


@router.get("/content")
async def content(user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    full = _has_full_access(sb, user)
    try:
        rows = (sb.table("academy_items").select("*")
                .eq("published", True)
                .order("starts_at", desc=True).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load academy: {exc}") from exc
    # server decides watchability; never leak full video_url to non-entitled
    now = _now()
    live, recordings, events = [], [], []
    for r in rows:
        watchable = full or r.get("access") == "preview"
        item = {
            "id": r["id"], "kind": r.get("kind"), "title": r.get("title"),
            "description": r.get("description"), "access": r.get("access"),
            "thumbnail": r.get("thumbnail"), "starts_at": r.get("starts_at"),
            "duration_min": r.get("duration_min"), "tags": r.get("tags") or [],
            "watchable": watchable,
            "video_url": r.get("video_url") if watchable else "",
        }
        k = r.get("kind")
        (live if k == "live" else events if k == "event" else recordings).append(item)
    # a live item is "on air" if its start is within the last 3h
    return {"full_access": full, "is_admin": _is_admin(user),
            "live": live, "recordings": recordings, "events": events,
            "server_time": now}


@router.post("/item")
async def create_item(item: ItemIn, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    row = {**item.model_dump(), "created_by": str(user.id), "created_at": _now()}
    try:
        res = sb.table("academy_items").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not create: {exc}") from exc
    return {"ok": True, "item": (res.data or [row])[0]}


@router.patch("/item/{item_id}")
async def update_item(item_id: str, patch: ItemPatch,
                      user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    upd = {k: v for k, v in patch.model_dump().items() if v is not None}
    upd["updated_at"] = _now()
    res = (sb.table("academy_items").update(upd).eq("id", item_id).execute())
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return {"ok": True, "item": res.data[0]}


@router.delete("/item/{item_id}")
async def delete_item(item_id: str, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    sb.table("academy_items").delete().eq("id", item_id).execute()
    return {"ok": True}
