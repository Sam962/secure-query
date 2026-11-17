"""Egress: stored values never reach the model provider.

Marker values are planted in a private column and in a column whose values are not
approved, then questions run end to end through `ask` with a client that records every
request. The model may be told that a value is missing; it is never shown a stored one,
and answer rows never go back to it.
"""

from __future__ import annotations

import json
import shutil

import duckdb
import pytest

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from tests.conftest import CHINOOK_DB, requires_chinook

PRIVATE = "zqxprivatemarker@example.invalid"
UNAPPROVED = "Zqx Unapproved Marker"


class RecordingClient:
    """Answers with canned SQL in turn (the last repeats) and keeps every request."""

    def __init__(self, *sql: str) -> None:
        self.responses = list(sql)
        self.sent: list[str] = []

    def complete(self, messages, response_format=None) -> str:
        self.sent.append(json.dumps(messages))
        sql = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return json.dumps({"sql": sql})


@pytest.fixture
def marked_db(tmp_path, monkeypatch):
    path = tmp_path / "chinook.duckdb"
    shutil.copy(CHINOOK_DB, path)
    with duckdb.connect(str(path)) as con:
        con.execute("UPDATE Customer SET Email = ?", [PRIVATE])
        con.execute("UPDATE Track SET Composer = ? WHERE TrackId <= 50", [UNAPPROVED])
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "duckdb")
    monkeypatch.setenv("SECURE_QUERY_DUCKDB", str(path))
    return path


def _ask(question: str, client: RecordingClient):
    return ask(
        question,
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=client,
    )


def _leaked(client: RecordingClient) -> list[str]:
    text = "\n".join(client.sent).lower()
    return [m for m in (PRIVATE, UNAPPROVED, "zqx") if m.lower() in text]


@requires_chinook
@pytest.mark.parametrize(
    "question, sql",
    [
        # Answer rows hold the unapproved marker: they must not go back to the model.
        ("Which composers wrote tracks?", ["SELECT DISTINCT Composer FROM Track ORDER BY Composer"]),
        # A guessed value close to a stored one: the person may see the stored value, the model not.
        (
            "How many tracks did Zq Unapproved write?",
            [
                "SELECT COUNT(*) AS n FROM Track WHERE Composer = 'Unapproved Marker'",
                "SELECT COUNT(*) AS n FROM Track WHERE Composer LIKE '%Unapproved%'",
            ],
        ),
        # A private column: refused by the kernel, and the refusal names no value.
        ("What are the customers' emails?", ["SELECT Email FROM Customer", "SELECT FirstName FROM Customer"]),
    ],
)
def test_no_stored_value_reaches_the_model(marked_db, question, sql) -> None:
    client = RecordingClient(*sql)
    outcome = _ask(question, client)
    assert client.sent, "the model was never called"
    assert _leaked(client) == [], f"stored value sent to the model ({outcome.status})"
    if question.startswith("Which composers"):
        assert UNAPPROVED in str(outcome.rows)  # the person sees it, so the markers are live
