import os

os.environ["DATABASE_URL"] = "postgresql://triage:triage@localhost:5433/triage_test"
os.environ["TRIAGE_WEBHOOK_SECRET"] = "test-secret"
os.environ["WORKER_ENABLED"] = "false"   # tests drive the worker by hand
os.environ["LLM_PROVIDER"] = "fake"
