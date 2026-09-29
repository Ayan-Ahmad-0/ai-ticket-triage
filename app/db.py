import os

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS triage;

CREATE TABLE IF NOT EXISTS triage.jobs (
    id          SERIAL PRIMARY KEY,
    source_id   TEXT UNIQUE NOT NULL,
    payload     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempts    INT NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS triage.drafts (
    id             SERIAL PRIMARY KEY,
    job_id         INT NOT NULL REFERENCES triage.jobs(id),
    category       TEXT,
    urgency        TEXT,
    sentiment      TEXT,
    has_pii        BOOLEAN,
    draft_reply    TEXT,
    tool_trace     JSONB NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'pending_approval',
    model          TEXT,
    prompt_version TEXT,
    cost_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    reviewed_by    TEXT,
    reviewed_at    TIMESTAMPTZ,
    reject_reason  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def get_conn():
    return psycopg.connect(
        os.environ.get("DATABASE_URL", "postgresql://triage:triage@localhost:5433/triage"),
        row_factory=dict_row,
    )


def init_db():
    with get_conn() as conn:
        conn.execute(SCHEMA)
