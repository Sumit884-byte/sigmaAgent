"""Conservative, auditable extraction of blocker and decision statements."""

from __future__ import annotations

import hashlib
import re

from models import ContextEvidence, DiscordMessage


BLOCKER_PATTERNS = (
    re.compile(r"\bblocked\s+by\b", re.IGNORECASE),
    re.compile(r"\bblocked\s+on\b", re.IGNORECASE),
    re.compile(r"\bwaiting\s+for\b", re.IGNORECASE),
    re.compile(r"\b(?:can't|cannot|unable to)\s+proceed\s+until\b", re.IGNORECASE),
)
DECISION_PATTERNS = (
    re.compile(r"\bdecision\s*:", re.IGNORECASE),
    re.compile(r"\bdecided\s+to\b", re.IGNORECASE),
    re.compile(r"\bagreed\s+to\b", re.IGNORECASE),
)


def extract_evidence(message: DiscordMessage) -> list[ContextEvidence]:
    """Keep a whole source sentence only when it explicitly signals evidence."""
    evidence: list[ContextEvidence] = []
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", message.content) if part.strip()]
    for sentence in sentences:
        kind = None
        if any(pattern.search(sentence) for pattern in BLOCKER_PATTERNS):
            kind = "blocker_candidate"
        elif any(pattern.search(sentence) for pattern in DECISION_PATTERNS):
            kind = "decision_candidate"
        if kind is None:
            continue
        digest = hashlib.sha256(f"{message.id}\0{kind}\0{sentence}".encode("utf-8")).hexdigest()[:24]
        evidence.append(
            ContextEvidence(
                id=digest,
                kind=kind,
                excerpt=sentence,
                source_message_id=message.id,
                source_url=message.source_url,
            )
        )
    return evidence


def explicit_task_reference(
    message: DiscordMessage,
    tasks: list[dict[str, str]],
    statement: str | None = None,
) -> str | None:
    """Return a task ID only when the evidence sentence names it explicitly."""
    text = statement if statement is not None else message.content
    content = text.casefold()
    content_tokens = re.findall(r"\w+", content, flags=re.UNICODE)
    for task in tasks:
        task_id = str(task.get("id") or "")
        title = str(task.get("title") or "")
        if task_id and re.search(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])", text, re.IGNORECASE):
            return task_id
        title_tokens = re.findall(r"\w+", title.casefold(), flags=re.UNICODE)
        if title_tokens and any(
            content_tokens[index:index + len(title_tokens)] == title_tokens
            for index in range(len(content_tokens) - len(title_tokens) + 1)
        ):
            return task_id or None
    return None
