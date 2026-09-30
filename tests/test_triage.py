import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from unittest.mock import patch

from app.db import get_conn
from app.llm import FakeProvider
from app.main import app, notify_helpdesk
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


def test_approval_page_shows_helpdesk_ticket_number(client):
    post_ticket(client, {**TICKET, "source_id": "helpdesk-32"})
    process_one(FakeProvider())

    response = client.get("/approvals")

    assert response.status_code == 200
    assert "Ticket no. 32" in response.text
    assert 'formaction="/approvals/1/approve"' in response.text


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


@pytest.mark.parametrize(
    ("action", "status"), [("approve", "approved"), ("reject", "rejected")]
)
def test_approval_sends_signed_helpdesk_status_callback(client, monkeypatch, action, status):
    monkeypatch.setenv("HELPDESK_URL", "https://helpdesk.example")
    monkeypatch.setenv("TRIAGE_WEBHOOK_SECRET", "callback-secret")
    with get_conn() as conn:
        job = conn.execute(
            "INSERT INTO triage.jobs (source_id, payload) VALUES (%s, %s) RETURNING id",
            ("helpdesk-42", Jsonb({"subject": "Test", "body": "Test body"})),
        ).fetchone()
        draft = conn.execute(
            "INSERT INTO triage.drafts (job_id) VALUES (%s) RETURNING id",
            (job["id"],),
        ).fetchone()

    form = {"reviewed_by": "Reviewer"}
    if action == "reject":
        form["reason"] = "Not applicable"
    with patch("app.main.httpx.post") as post:
        response = client.post(
            f"/approvals/{draft['id']}/{action}", data=form, follow_redirects=False
        )

    assert response.status_code == 303
    call = post.call_args
    assert call.args[0] == f"https://helpdesk.example/tickets/42/status"
    raw_body = call.kwargs["content"]
    assert raw_body == json.dumps({"status": status}, separators=(",", ":")).encode()
    expected = hmac.new(b"callback-secret", raw_body, hashlib.sha256).hexdigest()
    assert call.kwargs["headers"]["X-Signature"] == expected


def test_helpdesk_callback_accepts_local_development_url(monkeypatch):
    monkeypatch.setenv("HELPDESK_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("TRIAGE_WEBHOOK_SECRET", "callback-secret")
    with patch("app.main.httpx.post") as post:
        notify_helpdesk("helpdesk-42", "approved")
    assert post.call_args.args[0] == "http://127.0.0.1:8000/tickets/42/status"


def test_helpdesk_callback_rejects_remote_http_url(monkeypatch):
    monkeypatch.setenv("HELPDESK_URL", "http://helpdesk.example")
    monkeypatch.setenv("TRIAGE_WEBHOOK_SECRET", "callback-secret")
    with patch("app.main.httpx.post") as post:
        notify_helpdesk("helpdesk-42", "approved")
    post.assert_not_called()
