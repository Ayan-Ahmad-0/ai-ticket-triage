import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb

from app.db import get_conn, init_db
from app.llm import get_provider
from app.security import verify_signature
from app.worker import worker_loop
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

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
            "SELECT d.*, j.payload FROM triage.drafts d JOIN triage.jobs j ON j.id = d.job_id "
            "WHERE d.status = 'pending_approval' ORDER BY d.id"
        ).fetchall()
    return templates.TemplateResponse(request, "approvals.html", {"drafts": drafts})


@app.post("/approvals/{draft_id}/approve")
def approve(draft_id: int, reviewed_by: str = Form(...)):
    with get_conn() as conn:
        conn.execute(
            "UPDATE triage.drafts SET status='approved', reviewed_by=%s, reviewed_at=now() "
            "WHERE id=%s AND status='pending_approval'",
            (reviewed_by, draft_id),
        )
    return RedirectResponse("/approvals", status_code=303)


@app.post("/approvals/{draft_id}/reject")
def reject(draft_id: int, reviewed_by: str = Form(...), reason: str = Form(...)):
    with get_conn() as conn:
        conn.execute(
            "UPDATE triage.drafts SET status='rejected', reviewed_by=%s, reviewed_at=now(), "
            "reject_reason=%s WHERE id=%s AND status='pending_approval'",
            (reviewed_by, reason, draft_id),
        )
    return RedirectResponse("/approvals", status_code=303)
