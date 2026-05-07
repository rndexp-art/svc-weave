"""Telegram-specific ingest endpoint.

This is a wart — kept narrow on purpose. Replaces
services/telegram-bot/app/neo4j_writer.write_message verbatim. Refactoring
into a generic "ingest stream with chain edges" feature is post-v1.

Body shape mirrors what telegram-bot already produces, so its migration
is a one-line replacement (httpx call instead of direct neo4j driver).
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Caller, caller as require_caller
from .graph import session


router = APIRouter()


class _ExtractedMessage(BaseModel):
    chat_id: str
    chat_type: str | None = None
    chat_title: str | None = None
    user_external_id: str | None = None
    user_username: str | None = None
    user_first_name: str | None = None
    user_last_name: str | None = None
    message_external_id: str
    content: str
    sent_at: str | None = None
    is_edit: bool = False
    original_message_external_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class TelegramMessageIn(BaseModel):
    extracted: _ExtractedMessage
    write_context: dict[str, Any] = Field(default_factory=dict)


# Ported from services/telegram-bot/app/neo4j_writer.py.
# Bootstraps the telegram provider/source/identity, then creates the note
# (or :overrides chain for edits).
_TELEGRAM_INGEST = """
MERGE (tp:provider {name: 'telegram-bot'})
  ON CREATE SET tp.created_at = datetime(), tp.kind = 'telegram'

MERGE (s:source {provider_name: 'telegram-bot', external_id: $chat_id})
  ON CREATE SET s.created_at = datetime(),
                s.kind = coalesce($chat_type, 'unknown'),
                s.title = coalesce($chat_title, 'Telegram chat ' + $chat_id),
                s.chat_type = coalesce($chat_type, 'unknown')
SET s.title = coalesce($chat_title, s.title)
MERGE (s)-[:provider]->(tp)

WITH s, tp
FOREACH (_ IN CASE WHEN $user_external_id IS NOT NULL THEN [1] ELSE [] END |
    MERGE (i:identity {provider_name: 'telegram-bot', external_id: $user_external_id})
      ON CREATE SET i.created_at = datetime(), i.kind = 'telegram_user',
                    i.username = $user_username,
                    i.first_name = $user_first_name,
                    i.last_name = $user_last_name
    SET i.username   = coalesce($user_username,   i.username),
        i.first_name = coalesce($user_first_name, i.first_name),
        i.last_name  = coalesce($user_last_name,  i.last_name)
    MERGE (i)-[:provider]->(tp)
)

WITH s
OPTIONAL MATCH (i:identity {provider_name: 'telegram-bot', external_id: $user_external_id})
MERGE (n:input:note {provider_name: 'telegram-bot',
                     external_chat_id: $chat_id,
                     external_id:      $message_external_id})
  ON CREATE SET n.created_at = datetime(),
                n.note_id    = randomUUID(),
                n.content    = $content,
                n.sent_at    = CASE WHEN $sent_at = '' THEN null ELSE datetime($sent_at) END,
                n.meta       = $meta_json,
                n.is_edit    = $is_edit,
                n.original_external_id = $original_message_external_id
SET n.content = $content,
    n.meta    = $meta_json
MERGE (n)-[:source]->(s)
WITH n, i
FOREACH (_ IN CASE WHEN i IS NOT NULL THEN [1] ELSE [] END |
    MERGE (n)-[:author]->(i)
)
WITH n
OPTIONAL MATCH (orig:input:note {provider_name: 'telegram-bot',
                                 external_chat_id: $chat_id,
                                 external_id: $original_message_external_id})
    WHERE $original_message_external_id IS NOT NULL
FOREACH (_ IN CASE WHEN orig IS NOT NULL THEN [1] ELSE [] END |
    MERGE (n)-[:overrides]->(orig)
)
RETURN n.note_id AS note_id, elementId(n) AS element_id
"""


@router.post("/v1/integrations/telegram/messages")
def ingest_telegram(body: TelegramMessageIn,
                    caller: Annotated[Caller, Depends(require_caller)]) -> dict[str, Any]:
    if caller.kind != "service":
        raise HTTPException(403, "only service callers may use the telegram ingest endpoint")
    e = body.extracted
    params = {
        "chat_id":                       e.chat_id,
        "chat_type":                     e.chat_type,
        "chat_title":                    e.chat_title,
        "user_external_id":              e.user_external_id,
        "user_username":                 e.user_username,
        "user_first_name":               e.user_first_name,
        "user_last_name":                e.user_last_name,
        "message_external_id":           e.message_external_id,
        "content":                       e.content or "",
        "sent_at":                       e.sent_at or "",
        "meta_json":                     json.dumps(e.meta or {}, separators=(",", ":"), ensure_ascii=False, default=str),
        "is_edit":                       bool(e.is_edit),
        "original_message_external_id":  e.original_message_external_id,
    }
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(_TELEGRAM_INGEST, **params).single())
    if rec is None:
        raise HTTPException(500, "telegram ingest produced no record")
    return {"note_id": rec["note_id"], "element_id": rec["element_id"]}
