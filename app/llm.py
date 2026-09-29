import json
import os
import re
from dataclasses import dataclass

# $ per million tokens (input, output), paid tier.
PRICES = {"gemini-3.1-flash-lite": (0.30, 2.50)}
DEFAULT_MODEL = "gemini-3.1-flash-lite"


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model: str


class GeminiProvider:
    def __init__(self, model: str = DEFAULT_MODEL):
        from google import genai

        self.client = genai.Client()  # reads GEMINI_API_KEY or GOOGLE_API_KEY
        self.model = model

    def complete(
        self, system: str, user: str, json_mode: bool = False, response_schema=None
    ) -> LLMResult:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=800,
            response_mime_type="application/json" if json_mode else None,
            response_schema=response_schema,
        )
        resp = self.client.models.generate_content(
            model=self.model, contents=user, config=config
        )
        usage = resp.usage_metadata
        tin = (usage.prompt_token_count or 0) if usage else 0
        tout = (usage.candidates_token_count or 0) if usage else 0
        p_in, p_out = PRICES.get(self.model, (0, 0))
        cost = (tin * p_in + tout * p_out) / 1_000_000
        return LLMResult(resp.text or "", tin, tout, cost, self.model)


class FakeProvider:
    """Deterministic, free, offline. Used in tests and load tests."""

    def complete(
        self, system: str, user: str, json_mode: bool = False, response_schema=None
    ) -> LLMResult:
        if json_mode:
            t = user.lower()
            category = "billing" if ("refund" in t or "charged" in t) else "bug_report"
            urgency = "high" if "urgent" in t else "medium"
            text = json.dumps(
                {"category": category, "urgency": urgency, "sentiment": "negative", "has_pii": False}
            )
        else:
            text = "Hi, thanks for getting in touch. We are looking into this and will follow up shortly."
        return LLMResult(text, 100, 50, 0.0, "fake")


def get_provider():
    provider = os.environ.get("LLM_PROVIDER", "fake")
    if provider == "gemini":
        return GeminiProvider()
    return FakeProvider()


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)
