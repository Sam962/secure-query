"""Databricks Genie Conversation API client for head-to-head evals.

Blocked on a real workspace + space that can answer one question by hand.
This module talks to the documented Genie REST surface; it never claims a
score without live credentials and the same adjudicated grader as Secure Query.

Env:
  DATABRICKS_HOST
  DATABRICKS_TOKEN (or GENIE_TOKEN)
  GENIE_SPACE_ID
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


class GenieError(RuntimeError):
    """Genie API or configuration failure."""


@dataclass(frozen=True)
class GenieAnswer:
    """One Genie response normalized for the shared grader."""

    question: str
    status: str  # completed | failed | cancelled | timeout | refused
    text: str | None
    sql: str | None
    columns: list[str]
    rows: list[tuple[Any, ...]]
    conversation_id: str | None
    message_id: str | None
    raw: dict[str, Any]


def genie_configured() -> bool:
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    token = os.environ.get("GENIE_TOKEN") or os.environ.get("DATABRICKS_TOKEN")
    space = os.environ.get("GENIE_SPACE_ID")
    return bool(host and token and space)


def _settings() -> tuple[str, str, str]:
    host = (os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME") or "").rstrip("/")
    token = os.environ.get("GENIE_TOKEN") or os.environ.get("DATABRICKS_TOKEN") or ""
    space = os.environ.get("GENIE_SPACE_ID") or ""
    if not (host and token and space):
        raise GenieError(
            "Genie requires DATABRICKS_HOST, GENIE_TOKEN|DATABRICKS_TOKEN, GENIE_SPACE_ID"
        )
    if host.startswith("https://"):
        host = host[len("https://") :]
    elif host.startswith("http://"):
        host = host[len("http://") :]
    return f"https://{host}", token, space


def _request(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise GenieError(f"Genie HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GenieError(f"Genie network error: {exc}") from exc


def start_conversation(question: str) -> dict[str, Any]:
    base, token, space = _settings()
    return _request(
        "POST",
        f"{base}/api/2.0/genie/spaces/{space}/start-conversation",
        token,
        {"content": question},
    )


def get_message(conversation_id: str, message_id: str) -> dict[str, Any]:
    base, token, space = _settings()
    return _request(
        "GET",
        f"{base}/api/2.0/genie/spaces/{space}/conversations/{conversation_id}/messages/{message_id}",
        token,
    )


def get_query_result(conversation_id: str, message_id: str, attachment_id: str) -> dict[str, Any]:
    base, token, space = _settings()
    return _request(
        "GET",
        f"{base}/api/2.0/genie/spaces/{space}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/query-result",
        token,
    )


def _extract_ids(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    conversation = payload.get("conversation") or {}
    message = payload.get("message") or payload
    conversation_id = (
        payload.get("conversation_id")
        or conversation.get("id")
        or conversation.get("conversation_id")
    )
    message_id = (
        message.get("id")
        or message.get("message_id")
        or payload.get("message_id")
    )
    return (
        str(conversation_id) if conversation_id else None,
        str(message_id) if message_id else None,
    )


_INT_TYPES = frozenset({"BYTE", "SHORT", "INT", "INTEGER", "LONG", "BIGINT", "TINYINT", "SMALLINT"})
_FLOAT_TYPES = frozenset({"FLOAT", "DOUBLE", "DECIMAL", "REAL"})


def _typed(value: Any, type_name: str) -> Any:
    """The REST API sends every cell as a string; restore the type a SQL driver would return."""
    if value is None or not isinstance(value, str):
        return value
    try:
        if type_name in _INT_TYPES:
            return int(value)
        if type_name in _FLOAT_TYPES:
            return float(value)
        if type_name == "BOOLEAN":
            return value.lower() == "true"
        if type_name == "DATE":
            return date.fromisoformat(value)
        if type_name.startswith("TIMESTAMP"):
            return datetime.fromisoformat(value)
    except ValueError:
        return value
    return value


def _parse_result_table(attachment_result: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    statement = attachment_result.get("statement_response") or attachment_result
    manifest = statement.get("manifest") or {}
    schema_cols = (manifest.get("schema") or {}).get("columns") or []
    columns = [str(c.get("name") or c.get("column_name") or f"c{i}") for i, c in enumerate(schema_cols)]
    types = [str(c.get("type_name") or "").upper() for c in schema_cols]
    data = statement.get("result") or attachment_result.get("result") or {}
    rows: list[tuple[Any, ...]] = []
    for row in data.get("data_array") or data.get("data") or []:
        values = row["values"] if isinstance(row, dict) and "values" in row else row
        if isinstance(values, (list, tuple)):
            rows.append(tuple(_typed(v, types[i] if i < len(types) else "") for i, v in enumerate(values)))
    return columns, rows


def ask_genie(
    question: str,
    *,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 300.0,
) -> GenieAnswer:
    """Start a fresh conversation, poll to completion, return rows + SQL if any."""
    started = start_conversation(question)
    conversation_id, message_id = _extract_ids(started)
    if not conversation_id or not message_id:
        raise GenieError(f"Genie start-conversation missing ids: {started!r}")

    deadline = time.monotonic() + timeout_seconds
    message: dict[str, Any] = {}
    while time.monotonic() < deadline:
        message = get_message(conversation_id, message_id)
        status = str(message.get("status") or "").upper()
        if status in {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}:
            break
        time.sleep(poll_seconds)
    else:
        return GenieAnswer(
            question=question,
            status="timeout",
            text=None,
            sql=None,
            columns=[],
            rows=[],
            conversation_id=conversation_id,
            message_id=message_id,
            raw=message,
        )

    status = str(message.get("status") or "").lower()
    if status in {"failed", "cancelled"}:
        return GenieAnswer(
            question=question,
            status=status,
            text=str(message.get("error") or message.get("content") or status),
            sql=None,
            columns=[],
            rows=[],
            conversation_id=conversation_id,
            message_id=message_id,
            raw=message,
        )

    text_parts: list[str] = []
    sql: str | None = None
    columns: list[str] = []
    rows: list[tuple[Any, ...]] = []
    for att in message.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if att.get("text"):
            text_parts.append(str(att["text"]))
        query = att.get("query") or {}
        if isinstance(query, dict) and query.get("query"):
            sql = str(query["query"])
        attachment_id = att.get("attachment_id") or att.get("id")
        if attachment_id and (query or att.get("query_result")):
            try:
                result = get_query_result(conversation_id, message_id, str(attachment_id))
                columns, rows = _parse_result_table(result)
            except GenieError:
                pass

    return GenieAnswer(
        question=question,
        status="completed",
        text="\n".join(text_parts) or None,
        sql=sql,
        columns=columns,
        rows=rows,
        conversation_id=conversation_id,
        message_id=message_id,
        raw=message,
    )
