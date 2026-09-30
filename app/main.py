import asyncio
import hashlib
import hmac
import json
import logging
import os
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb

from app.db import get_conn, init_db
from app.llm import get_provider
from app.security import verify_signature
from app.worker import worker_loop

load_dotenv(Path(__file__).parent.parent / ".env")
log = logging.getLogger("uvicorn.error")


def notify_helpdesk(source_id: str, status: str) -> None:
    if not source_id.startswith("helpdesk-"):
        return  # this job didn't originate from the helpdesk repo
    ticket_id = source_id.removeprefix("helpdesk-")
    if not ticket_id.isascii() or not ticket_id.isdigit():
        log.error("cannot notify helpdesk: invalid ticket ID in source_id %r", source_id)
        return
    helpdesk_url = os.environ.get("HELPDESK_URL", "").strip().rstrip("/")
    parsed_url = urlsplit(helpdesk_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        log.error("cannot notify helpdesk: HELPDESK_URL must be an HTTP or HTTPS URL")
        return
    try:
        is_loopback = ipaddress.ip_address(parsed_url.hostname).is_loopback
    except ValueError:
        is_loopback = parsed_url.hostname.lower() == "localhost"
    if parsed_url.scheme != "https" and not is_loopback:
        log.error("cannot notify helpdesk: non-local HELPDESK_URL must use HTTPS")
        return
    if status not in {"approved", "rejected"}:
        log.error("cannot notify helpdesk: unsupported status %r", status)
        return
    secret = os.environ.get("TRIAGE_WEBHOOK_SECRET", "")
    if not secret:
        log.error("cannot notify helpdesk: TRIAGE_WEBHOOK_SECRET is not configured")
        return
    body = json.dumps({"status": status}, separators=(",", ":")).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        response = httpx.post(
            f"{helpdesk_url}/tickets/{ticket_id}/status",
            content=body,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
            timeout=5,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.exception("helpdesk status callback failed for ticket %s", ticket_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = None
    if os.environ.get("WORKER_ENABLED", "true") == "true":
        provider = get_provider()
        print(f"triage worker started, provider={type(provider).__name__}", flush=True)
        task = asyncio.create_task(worker_loop(provider))
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhooks/tickets", status_code=202)
async def ticket_webhook(request: Request):
    raw = await request.body()
    secret = os.environ["TRIAGE_WEBHOOK_SECRET"]
    if not verify_signature(raw, request.headers.get("X-Signature", ""), secret):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = json.loads(raw)
        source_id = str(payload["source_id"])
        payload["subject"], payload["body"]  # required fields
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=422, detail="invalid payload")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO triage.jobs (source_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (source_id) DO NOTHING",
            (source_id, Jsonb(payload)),
        )
    return {"status": "accepted"}  # same answer for a replay: that is idempotency


@app.get("/approvals")
def approvals(request: Request):
    with get_conn() as conn:
        drafts = conn.execute(
            "SELECT d.*, j.payload, j.source_id FROM triage.drafts d "
            "JOIN triage.jobs j ON j.id = d.job_id "
            "WHERE d.status = 'pending_approval' ORDER BY d.id"
        ).fetchall()
    return templates.TemplateResponse(request, "approvals.html", {"drafts": drafts})


@app.post("/approvals/{draft_id}/approve")
def approve(draft_id: int, reviewed_by: str = Form(...)):
    source_id = None
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE triage.drafts SET status='approved', reviewed_by=%s, reviewed_at=now() "
            "WHERE id=%s AND status='pending_approval' RETURNING job_id",
            (reviewed_by, draft_id),
        ).fetchone()
        if row:
            job = conn.execute(
                "SELECT source_id FROM triage.jobs WHERE id=%s", (row["job_id"],)
            ).fetchone()
            if job:
                source_id = job["source_id"]
    if source_id:
        notify_helpdesk(source_id, "approved")
    return RedirectResponse("/approvals", status_code=303)


@app.post("/approvals/{draft_id}/reject")
def reject(draft_id: int, reviewed_by: str = Form(...), reason: str = Form(...)):
    source_id = None
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE triage.drafts SET status='rejected', reviewed_by=%s, reviewed_at=now(), "
            "reject_reason=%s WHERE id=%s AND status='pending_approval' RETURNING job_id",
            (reviewed_by, reason, draft_id),
        ).fetchone()
        if row:
            job = conn.execute(
                "SELECT source_id FROM triage.jobs WHERE id=%s", (row["job_id"],)
            ).fetchone()
            if job:
                source_id = job["source_id"]
    if source_id:
        notify_helpdesk(source_id, "rejected")
    return RedirectResponse("/approvals", status_code=303)