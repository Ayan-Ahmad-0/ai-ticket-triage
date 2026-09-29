from pathlib import Path

from app.classify import TicketClassification
from app.llm import LLMResult

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "draft_v1.md"
PROMPT_VERSION = "draft_v1"


def write_draft(provider, subject: str, body: str, c: TicketClassification) -> LLMResult:
    system = PROMPT_PATH.read_text()
    user = (
        f"Classification: category={c.category}, urgency={c.urgency}, sentiment={c.sentiment}\n\n"
        f"<ticket>\nSubject: {subject}\n\n{body}\n</ticket>"
    )
    return provider.complete(system, user)
