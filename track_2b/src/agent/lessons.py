"""Lesson layer: reviewer notes -> masked -> distilled by Apertus -> next manual generation.

Not training: the model weights never change. It is a hand-over memo that is
appended to the manual, fingerprinted (sha256) and recorded in the ledger.
"""
import re

from . import llm, prompts
from .common import sha256

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
AMOUNT_RE = re.compile(r"CHF\s?[\d'’ ,.]+\d|[\d'’ ,.]+\d\s?CHF")
INVOICE_RE = re.compile(r"\b[A-Z]{1,3}-20\d\d-\d{3,6}\b")


def mask(text: str, names: list) -> str:
    text = IBAN_RE.sub("[IBAN]", text)
    text = AMOUNT_RE.sub("[AMOUNT]", text)
    text = INVOICE_RE.sub("[INVOICE-NO]", text)
    for n in sorted(set(names), key=len, reverse=True):
        text = text.replace(n, "[COMPANY]")
    return text


def distill(masked_notes: list) -> list:
    """Ask Apertus to turn reviewer notes into general manual rules."""
    if not masked_notes:
        return []
    notes = "\n".join(f"- {n}" for n in sorted(set(masked_notes)))
    msgs = [{"role": "system", "content": prompts.DISTILL_SYSTEM},
            {"role": "user", "content": prompts.DISTILL_USER.format(notes=notes)}]
    try:
        out = llm.parse_json(llm.chat(msgs, max_tokens=500, tag="distill"))
        rules = [str(r).strip() for r in out.get("rules", []) if str(r).strip()][:5]
    except Exception:
        rules = []
    # fall back to the masked notes themselves so a lesson is never silently lost
    return rules or sorted(set(masked_notes))[:5]


def manual_with_lessons(rules: list, generation: int) -> str:
    if not rules:
        return prompts.MANUAL_GEN0
    lines = "\n".join(f"L{i + 1}. {r}" for i, r in enumerate(rules))
    return f"{prompts.MANUAL_GEN0}\n\nLessons from reviewer feedback (generation {generation}):\n{lines}"


def fingerprint(text: str) -> str:
    return sha256(text)
