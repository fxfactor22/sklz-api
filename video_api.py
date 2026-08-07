"""SKLZ — video generation via Replicate.

The API key stays in Railway's environment and is never sent to the browser or
pasted into a conversation. The client asks this endpoint to start a job and
polls for the result; the credential never leaves the server.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth import get_current_user

router = APIRouter(prefix="/api/video", tags=["video"])

# a video model that handles abstract motion well and is not absurdly slow
DEFAULT_MODEL = "minimax/video-01"


def _token() -> str:
    return os.environ.get("REPLICATE_API_TOKEN", "").strip()


def _owner_only(user) -> None:
    owner = os.environ.get("OWNER_EMAIL", "fxfactor24@gmail.com").strip().lower()
    if (getattr(user, "email", "") or "").strip().lower() != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Video generation is limited to the account owner.")


def _call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    tok = _token()
    if not tok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "REPLICATE_API_TOKEN is not set in the environment.")
    url = path if path.startswith("http") else f"https://api.replicate.com/v1{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        # named agent: several APIs sit behind Cloudflare, which rejects the
        # default Python-urllib signature outright
        "User-Agent": "SKLZ-Labs/1.0 (+https://www.sklzlabs.com)",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            pass
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Replicate returned {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not reach Replicate: {str(exc)[:160]}") from exc


class GenIn(BaseModel):
    prompt: str = Field(min_length=10, max_length=1200)
    model: str = DEFAULT_MODEL


@router.get("/status")
async def status_check(user=Depends(get_current_user)) -> dict:
    _owner_only(user)
    if not _token():
        return {"configured": False,
                "message": "REPLICATE_API_TOKEN is not set."}
    try:
        me = _call("/account")
        return {"configured": True, "account": me.get("username", ""),
                "message": "Ready."}
    except HTTPException as exc:
        return {"configured": True, "message": f"Token present but rejected: "
                                               f"{exc.detail}"}


@router.post("/generate")
async def generate(body: GenIn, user=Depends(get_current_user)) -> dict:
    """Start a generation. Returns immediately with an id to poll."""
    _owner_only(user)
    out = _call("/predictions", "POST", {
        "model": body.model,
        "input": {"prompt": body.prompt, "prompt_optimizer": False},
    })
    return {"ok": True, "id": out.get("id"), "status": out.get("status"),
            "note": ("Started. Poll /api/video/result/{id} — abstract clips "
                     "usually take two to five minutes.")}


@router.get("/result/{pred_id}")
async def result(pred_id: str, user=Depends(get_current_user)) -> dict:
    _owner_only(user)
    out = _call(f"/predictions/{pred_id}")
    st = out.get("status")
    url = out.get("output")
    if isinstance(url, list):
        url = url[0] if url else None
    return {"status": st, "url": url,
            "error": out.get("error") or "",
            "done": st in ("succeeded", "failed", "canceled")}
