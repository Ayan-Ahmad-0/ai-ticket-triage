import re

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s\-().]{8,}\d")


def _phone_sub(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    return "[PHONE]" if 10 <= len(digits) <= 13 else m.group()


def redact(text: str) -> str:
    text = EMAIL.sub("[EMAIL]", text)
    return PHONE_CANDIDATE.sub(_phone_sub, text)
