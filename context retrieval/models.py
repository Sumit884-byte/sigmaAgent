"""Graph-facing models for Discord history and extracted evidence."""

from __future__ import annotations

from pydantic import BaseModel, Field

DISCORD_CHANNEL_ID_PATTERN = r"^[0-9]{17,20}$"


class DiscordMessage(BaseModel):
    id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    channel_name: str = ""
    guild_id: str | None = None
    guild_name: str | None = None
    author_id: str = ""
    author_name: str = Field(min_length=1)
    content: str = ""
    timestamp: str = ""
    source_url: str = ""


class ContextEvidence(BaseModel):
    id: str
    kind: str
    excerpt: str
    source_message_id: str
    source_url: str


class ContextRequest(BaseModel):
    """Ticket fields from Person 2, used to find related conversation context."""

    feature_title: str = Field(min_length=1, max_length=300)
    task_id: str | None = Field(default=None, max_length=100)
    blocker: str | None = Field(default=None, max_length=500)
    assignee: str | None = Field(default=None, max_length=200)
    channel_id: str | None = Field(
        default=None, min_length=17, max_length=20, pattern=DISCORD_CHANNEL_ID_PATTERN
    )
    limit: int = Field(default=20, ge=1, le=100)
