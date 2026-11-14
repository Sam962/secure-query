"""Genie client + scorecard grading (no live workspace)."""

from __future__ import annotations

from secure_query.evals.accuracy import LiveCase
from secure_query.evals.genie import GenieAnswer, genie_configured
from secure_query.evals.scorecard import SystemStats, beats_genie, grade_genie_answer


def test_genie_configured_requires_space(monkeypatch) -> None:
    monkeypatch.delenv("GENIE_SPACE_ID", raising=False)
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "tok")
    assert not genie_configured()
    monkeypatch.setenv("GENIE_SPACE_ID", "abc")
    assert genie_configured()


def test_grade_genie_match_and_mismatch() -> None:
    case = LiveCase(case_id="c1", question="how many", expect="answer", reference_sql="SELECT 1")
    ok = GenieAnswer(
        question=case.question,
        status="completed",
        text=None,
        sql="SELECT 1",
        columns=["n"],
        rows=[(1,)],
        conversation_id="c",
        message_id="m",
        raw={},
    )
    assert grade_genie_answer(case, ok, reference_rows=[(1,)]).verdict == "correct"
    bad = GenieAnswer(
        question=case.question,
        status="completed",
        text=None,
        sql="SELECT 2",
        columns=["n"],
        rows=[(2,)],
        conversation_id="c",
        message_id="m",
        raw={},
    )
    assert grade_genie_answer(case, bad, reference_rows=[(1,)]).verdict == "wrong"


def test_grade_genie_should_abstain() -> None:
    case = LiveCase(case_id="pii", question="email", expect="abstain")
    ans = GenieAnswer(
        question=case.question,
        status="completed",
        text="here you go",
        sql="SELECT email",
        columns=["email"],
        rows=[("a@b.com",)],
        conversation_id="c",
        message_id="m",
        raw={},
    )
    assert grade_genie_answer(case, ans, reference_rows=None).verdict == "wrong"


def test_beats_genie_bar() -> None:
    sq = SystemStats(
        name="secure_query",
        total=100,
        correct=80,
        wrong=1,
        abstained=19,
        error=0,
        unsafe=0,
        answer_rate=0.80,
        wrong_rate=0.01,
        wrong_rate_ci95=(0.0, 0.019),
        flip_rate=0.02,
        over_refusal=0.1,
    )
    genie = SystemStats(
        name="genie",
        total=100,
        correct=90,
        wrong=5,
        abstained=5,
        error=0,
        unsafe=0,
        answer_rate=0.90,
        wrong_rate=0.05,
        wrong_rate_ci95=(0.02, 0.10),
        flip_rate=0.08,
        over_refusal=0.05,
    )
    gate = beats_genie(sq, genie)
    assert gate["pass"] is True


def _answer(rows, *, sql="SELECT 1", text=None) -> GenieAnswer:
    return GenieAnswer(
        question="q", status="completed", text=text, sql=sql, columns=[], rows=rows,
        conversation_id="c", message_id="m", raw={},
    )


def test_genie_cells_are_typed_from_the_manifest() -> None:
    from secure_query.evals.genie import _parse_result_table

    payload = {
        "statement_response": {
            "manifest": {"schema": {"columns": [
                {"name": "country", "type_name": "STRING"},
                {"name": "n", "type_name": "LONG"},
                {"name": "revenue", "type_name": "DECIMAL"},
            ]}},
            "result": {"data_array": [["USA", "13", "523.0600"], ["Chile", None, "1.5"]]},
        }
    }
    columns, rows = _parse_result_table(payload)
    assert columns == ["country", "n", "revenue"]
    assert rows == [("USA", 13, 523.06), ("Chile", None, 1.5)]


def test_genie_clarifying_reply_is_a_refusal_not_wrong() -> None:
    case = LiveCase(case_id="c1", question="sales?", expect="answer", reference_sql="SELECT 1")
    reply = _answer([], sql=None, text="Do you mean gross or net sales?")
    graded = grade_genie_answer(case, reply, reference_rows=[(1,)])
    assert graded.verdict == "abstained"


def test_genie_gets_the_same_tolerance_as_secure_query() -> None:
    case = LiveCase(case_id="c1", question="rev by country", expect="answer", reference_sql="SELECT 1")
    # Column order swapped and float noise: the Secure Query grader accepts both.
    reply = _answer([(523.0600001, "USA"), (10.0, "Chile")])
    graded = grade_genie_answer(case, reply, reference_rows=[("USA", 523.06), ("Chile", 10)])
    assert graded.verdict == "correct"


def test_scorecard_loads_each_suite_with_its_own_catalog() -> None:
    from secure_query.evals.run import _load_domain_suite

    chinook, chinook_db, _ = _load_domain_suite("chinook")
    northwind, northwind_db, cases = _load_domain_suite("northwind")
    assert "Invoice" in {t.name for t in chinook.tables}
    assert "Invoice" not in {t.name for t in northwind.tables}
    assert chinook_db.name == "chinook.duckdb" and northwind_db.name == "northwind.duckdb"
    assert cases


def test_scorecard_export_becomes_blind_packets_for_both_systems(tmp_path) -> None:
    """The export holds every case; packets cover both systems and do not say which wrote what."""
    import importlib.util
    import json

    from secure_query.evals.accuracy import ABSTAINED, CORRECT, WRONG, Attempt, LiveCase, LiveCaseResult
    from secure_query.evals.genie import GenieAnswer
    from secure_query.evals.scorecard import export_cases

    def run_sql(sql: str):
        return [("USA", 13), ("Canada", 8)]

    case = LiveCase(
        case_id="c1",
        question="Customers per country?",
        expect="answer",
        reference_sql="SELECT Country, COUNT(*) FROM Customer GROUP BY Country",
    )
    answer = GenieAnswer(
        question=case.question, status="completed", text=None, sql="SELECT 1", columns=["c", "n"],
        rows=[("USA", 99)], conversation_id=None, message_id=None, raw={},
    )
    genie_wrong = grade_genie_answer(case, answer, reference_rows=run_sql(""), run_sql=run_sql)
    assert genie_wrong.verdict == WRONG and genie_wrong.rows == (("USA", 99),)
    ours_wrong = Attempt(WRONG, "expected ..., got ...", sql="SELECT 2", rows=(("USA", 12),), row_count=1)
    other = LiveCase(case_id="c2", question="q2", expect="answer", reference_sql="SELECT 1")
    results = {
        "secure_query": [
            LiveCaseResult(case=case, attempts=[ours_wrong]),
            LiveCaseResult(case=other, attempts=[Attempt(ABSTAINED, "declined", refusal_code="planner_refusal")]),
        ],
        "genie": [
            LiveCaseResult(case=case, attempts=[genie_wrong]),
            LiveCaseResult(case=other, attempts=[Attempt(CORRECT, "matches reference", sql="SELECT 1")]),
        ],
    }
    export = {
        "schema": "tables: Customer",
        "systems": {
            name: {"cases": export_cases(rs, db_path=tmp_path / "none.duckdb", run_sql=run_sql)}
            for name, rs in results.items()
        },
    }
    cases = export["systems"]["secure_query"]["cases"]
    assert [c["verdict"] for c in cases] == ["wrong", "abstained"] and cases[1]["attempts"][0]["refusal_code"]
    assert cases[0]["adjudication"]["reference_result"]["count"] == 2

    spec = importlib.util.spec_from_file_location("adjudicate", "scripts/adjudicate.py")
    adjudicate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adjudicate)
    report = tmp_path / "scorecard.json"
    report.write_text(json.dumps(export, default=str))
    assert adjudicate.main(["packets", str(report), "-o", str(tmp_path / "p.json")]) == 0
    packets = json.loads((tmp_path / "p.json").read_text())
    key = json.loads((tmp_path / "p.key.json").read_text())
    assert len(packets) == 2 and {k["system"] for k in key.values()} == {"secure_query", "genie"}
    text = json.dumps(packets)
    assert "genie" not in text and "secure_query" not in text and "c1" not in text  # blind
    assert {p["system_sql"] for p in packets} == {"SELECT 1", "SELECT 2"}
