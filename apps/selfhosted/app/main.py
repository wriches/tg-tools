"""FastAPI application: auth flow + read-only scan, plus the static frontend.

Run from apps/selfhosted/:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from tg_tools_core.exceptions import LoginError, NotAuthorizedError
from tg_tools_core.models import (
    AuditEntry,
    RemovalOutcome,
    ScanResult,
    SendOutcome,
    TargetProfile,
)

from . import db
from .config import ConfigError, get_settings
from .schemas import (
    AuthStepResponse,
    ContactSendRequest,
    PasswordRequest,
    RemoveRequest,
    ScanRequest,
    SendCodeRequest,
    SignInRequest,
    StatusResponse,
)
from .service import service

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.init(get_settings().resolved_db_path)
    except ConfigError as exc:
        # Clean, actionable message instead of a pydantic stack trace on first run.
        raise SystemExit(f"\nConfiguration error:\n{exc}") from None
    yield


app = FastAPI(title="tg-tools (self-hosted)", version="0.1.0", lifespan=lifespan)


@app.get("/api/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    authorized, me = await service.status()
    return StatusResponse(authorized=authorized, me=me)


@app.post("/api/auth/send_code")
async def send_code(req: SendCodeRequest) -> dict:
    try:
        await service.send_code(req.phone)
    except (LoginError, NotAuthorizedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"next": "code"}


@app.post("/api/auth/sign_in", response_model=AuthStepResponse)
async def sign_in(req: SignInRequest) -> AuthStepResponse:
    try:
        me = await service.sign_in_code(req.code)
    except (LoginError, NotAuthorizedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AuthStepResponse(next="password") if me is None else AuthStepResponse(next="done", me=me)


@app.post("/api/auth/password", response_model=AuthStepResponse)
async def password(req: PasswordRequest) -> AuthStepResponse:
    try:
        me = await service.sign_in_password(req.password)
    except (LoginError, NotAuthorizedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AuthStepResponse(next="done", me=me)


@app.post("/api/auth/logout")
async def logout() -> dict:
    await service.logout()
    return {"ok": True}


@app.post("/api/profile", response_model=TargetProfile)
async def profile(req: ScanRequest) -> TargetProfile:
    try:
        return await service.get_profile(req.handle)
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Profile lookup failed: {exc}")


@app.post("/api/scan", response_model=ScanResult)
async def scan(req: ScanRequest) -> ScanResult:
    try:
        return await service.scan(req.handle)
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Scan failed: {exc}")


@app.websocket("/ws/scan")
async def ws_scan(ws: WebSocket) -> None:
    """Scan with live progress. Client sends {handle}; server streams
    {type:"progress", done, total} messages, then a {type:"result"} or
    {type:"error"} frame."""
    await ws.accept()
    try:
        req = await ws.receive_json()
    except Exception:
        await ws.close()
        return
    handle = (req or {}).get("handle", "")

    async def on_progress(done: int, total: int, wait: int | None = None) -> None:
        await ws.send_json({"type": "progress", "done": done, "total": total, "wait": wait})

    try:
        result = await service.scan(handle, on_progress)
        await ws.send_json({"type": "result", "data": result.model_dump()})
    except (NotAuthorizedError, ValueError) as exc:
        await ws.send_json({"type": "error", "detail": str(exc)})
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "detail": f"Scan failed: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.post("/api/remove")
async def remove(req: RemoveRequest) -> dict[str, list[RemovalOutcome]]:
    if not req.group_ids:
        raise HTTPException(status_code=400, detail="No groups selected.")
    try:
        outcomes = await service.remove_target(req.target_id, req.group_ids, req.ban)
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Removal failed: {exc}")
    return {"outcomes": outcomes}


@app.post("/api/contact/send", response_model=SendOutcome)
async def contact_send(req: ContactSendRequest) -> SendOutcome:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Message is empty.")
    try:
        return await service.send_message(req.admin_id, req.text, req.target_handle)
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Send failed: {exc}")


@app.get("/api/audit")
async def audit() -> dict[str, list[AuditEntry]]:
    entries = [AuditEntry(**e) for e in db.audit_list()]
    return {"entries": entries}


# Serve the static frontend last so /api/* routes take precedence.
_frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
