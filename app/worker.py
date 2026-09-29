import asyncio
import logging
import os

import httpx
from psycopg.types.json import Jsonb

from app import classify as classify_mod
from app import draft as draft_mod
from app.db import get_conn
from app.redact import redact

log = logging.getLogger("uvicorn.error")
MAX_ATTEMPTS = 6


def alert_failure(job_id: int, error: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        httpx.post(url, json={"content": f"triage job {job_id} failed: {error[:300]}"}, timeout=5)
    except Exception:
        log.exception("discord alert failed")


def process_one(provider) -> bool:
    """Take one queued job, process it, commit. Returns False when the queue is empty."""
    with get_conn() as conn:
        job = conn.execute(
            "SELECT * FROM triage.jobs WHERE status = 'queued' "
            "AND (attempts = 0 OR updated_at <= now() - CASE attempts "
            "WHEN 1 THEN interval '15 seconds' "
            "WHEN 2 THEN interval '30 seconds' "
            "WHEN 3 THEN interval '1 minute' "
            "WHEN 4 THEN interval '2 minutes' "
            "ELSE interval '5 minutes' END) "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not job:
            return False
        try:
            with conn.transaction():  # savepoint: a failure rolls back only this block
                payload = job["payload"]
                # Redaction happens first: nothing below sees raw personal data.
                subject = redact(payload["subject"])
                body = redact(payload["body"])
                cls, r1 = classify_mod.classify(provider, subject, body)
                r2 = draft_mod.write_draft(provider, subject, body, cls)
                has_pii = cls.has_pii or subject != payload["subject"] or body != payload["body"]
                conn.execute(
                    "INSERT INTO triage.drafts (job_id, category, urgency, sentiment, has_pii, "
                    "draft_reply, tool_trace, model, prompt_version, cost_usd) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        job["id"], cls.category, cls.urgency, cls.sentiment, has_pii,
                        r2.text,
                        Jsonb({"subject": subject, "body": body, "tool_rounds": []}),
                        r2.model,
                        f"{classify_mod.PROMPT_VERSION}+{draft_mod.PROMPT_VERSION}",
                        r1.cost_usd + r2.cost_usd,
                    ),
                )
                conn.execute(
                    "UPDATE triage.jobs SET status='done', updated_at=now() WHERE id=%s",
                    (job["id"],),
                )
        except Exception as e:
            attempts = job["attempts"] + 1
            status = "queued" if attempts < MAX_ATTEMPTS else "failed"
            conn.execute(
                "UPDATE triage.jobs SET attempts=%s, status=%s, last_error=%s, updated_at=now() "
                "WHERE id=%s",
                (attempts, status, str(e), job["id"]),
            )
            if status == "failed":
                alert_failure(job["id"], str(e))
        return True


async def worker_loop(provider):
    while True:
        try:
            did_work = await asyncio.to_thread(process_one, provider)
        except Exception:
            log.exception("worker iteration crashed")
            did_work = False
        if not did_work:
            await asyncio.sleep(2)
