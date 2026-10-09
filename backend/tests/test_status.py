import os

from app.db import Database
from app.jobs import Jobs


def test_migrations_run_once_and_in_order(tmp_path):
    path = str(tmp_path / "agent.db")
    db = Database(path)
    scripts = ["CREATE TABLE a (x INTEGER)", "ALTER TABLE a ADD COLUMN y TEXT; CREATE INDEX a_x ON a (x)"]
    db.migrate("mod", scripts[:1])
    db.execute("INSERT INTO a (x) VALUES (?)", (1,))
    db.migrate("mod", scripts)       # only the second script runs
    db.migrate("mod", scripts)       # nothing runs
    db.execute("INSERT INTO a (x, y) VALUES (?, ?)", (2, "b"))
    db.close()
    again = Database(path)            # the file remembers what ran
    again.migrate("mod", scripts)
    assert again.query("SELECT x, y FROM a ORDER BY x") == [{"x": 1, "y": None}, {"x": 2, "y": "b"}]
    assert again.one("SELECT version FROM _migrations WHERE module = 'mod'") == {"version": 2}


def test_a_failing_migration_leaves_nothing_half_done():
    db = Database("memory")
    try:
        db.migrate("bad", ["CREATE TABLE ok (x INTEGER); CREATE TABLE ok (x INTEGER)"])
    except Exception:
        pass
    assert db.query("SELECT name FROM sqlite_master WHERE name = 'ok'") == []
    assert db.one("SELECT version FROM _migrations WHERE module = 'bad'") is None


def test_job_states():
    j = Jobs()
    j.declare("a", "A", 60)
    j.declare("b", "B", 60)
    j.declare("c", "C", 60, enabled=False)
    j.ok("a", "ran")
    j.fail("b", RuntimeError("boom"))
    j.ok("unknown")  # undeclared jobs are ignored
    by = {r["name"]: r for r in j.status()}
    assert by["a"]["state"] == "ok" and by["a"]["detail"] == "ran" and by["a"]["runs"] == 1
    assert by["b"]["state"] == "error" and by["b"]["last_error"] == "boom"
    assert by["c"]["state"] == "off"
    j.ok("b")
    assert {r["name"]: r for r in j.status()}["b"]["state"] == "ok"
    # Nothing reported for longer than 3x its period (and past the start-up grace): overdue.
    late = {r["name"]: r for r in j.status(now=by["a"]["last_ok"] + 3600)}
    assert late["a"]["state"] == "overdue"


def test_status_endpoint():
    os.environ["DATA_SOURCE"] = "synthetic"
    os.environ["LLM_PROVIDER"] = "none"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/status").json()
    assert body["llm"]["configured"] is False and body["llm"]["problem"]
    assert body["database"]["memory"] is True
    assert {"alerts", "trade_manager", "brief"} <= {j["name"] for j in body["jobs"]}
    assert set(body["channels"]) == {"telegram", "discord"}
