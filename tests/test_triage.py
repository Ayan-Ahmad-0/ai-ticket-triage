import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.llm import FakeProvider
from app.main import app
from app.worker import process_one

SECRET = "test-secret"


def sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def post_ticket(client, payload, signature=None):
    raw = json.dumps(payload).encode()
    return client.post(
        "/webhooks/tickets",
        content=raw,
        headers={"X-Signature": signature or sign(raw), "Content-Type": "application/json"},
    )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with get_conn() as conn:
            conn.execute("TRUNCATE triage.drafts, triage.jobs RESTART IDENTITY CASCADE")
        yield c


TICKET = {
    "source_id": "helpdesk-1",
    "subject": "Refund please",
    "body": "I was charged twice. Call me on 0300-1234567 or mail a@example.com",
    "sender_email": "a@example.com",
}


def test_bad_signature_is_rejected(client):
    r = post_ticket(client, TICKET, signature="deadbeef")
    assert r.status_code == 401
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM triage.jobs").fetchone()["n"] == 0


def test_replay_five_times_creates_one_job(client):
    for _ in range(5):
        assert post_ticket(client, TICKET).status_code == 202
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM triage.jobs").fetchone()["n"] == 1


def test_end_to_end_draft_is_pending_and_redacted(client):
    post_ticket(client, TICKET)
    assert process_one(FakeProvider()) is True
    with get_conn() as conn:
        d = conn.execute("SELECT * FROM triage.drafts").fetchone()
    assert d["status"] == "pending_approval"
    assert d["category"] == "billing"
    assert d["has_pii"] is True
    trace = json.dumps(d["tool_trace"])
    assert "[EMAIL]" in trace and "[PHONE]" in trace
    assert "a@example.com" not in trace and "1234567" not in trace


def test_failed_job_waits_for_retry_backoff(client):
    post_ticket(client, {**TICKET, "source_id": "retry-backoff"})
    with get_conn() as conn:
        conn.execute(
            "UPDATE triage.jobs SET attempts=1, updated_at=now() WHERE source_id=%s",
            ("retry-backoff",),
        )
    assert process_one(FakeProvider()) is False
    with get_conn() as conn:
        conn.execute(
            "UPDATE triage.jobs SET updated_at=now() - interval '16 seconds' "
            "WHERE source_id=%s",
            ("retry-backoff",),
        )
    assert process_one(FakeProvider()) is True
