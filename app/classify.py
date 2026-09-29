from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.llm import LLMResult, parse_json

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classify_v1.md"
PROMPT_VERSION = "classify_v1"


class TicketClassification(BaseModel):
    category: Literal[
        "billing", "account_access", "bug_report", "feature_request", "how_to", "other"
    ]
    urgency: Literal["low", "medium", "high"]
    sentiment: Literal["negative", "neutral", "positive"]
    has_pii: bool


def classify(provider, subject: str, body: str) -> tuple[TicketClassification, LLMResult]:
    system = PROMPT_PATH.read_text()
    user = f"<ticket>\nSubject: {subject}\n\n{body}\n</ticket>"
    result = provider.complete(
        system, user, json_mode=True, response_schema=TicketClassification
    )
    return TicketClassification(**parse_json(result.text)), result
