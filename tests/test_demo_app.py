from fastapi.testclient import TestClient

from fde_kit.demo_app import DemoState, create_demo_app


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_DB_PATH", str(tmp_path / "demo.db"))
    return TestClient(create_demo_app(DemoState()))


def test_index_renders_eval_table_and_sections(tmp_path, monkeypatch):
    r = client(tmp_path, monkeypatch).get("/")
    assert r.status_code == 200
    for needle in (
        "eval-table",
        "Run the hello agent",
        "Review queue",
        "FDE_SQL_URL",
        "Demo data resets periodically",
        "github.com/cmzaia/fde-agent-kit",
    ):
        assert needle in r.text


def test_run_returns_trace(tmp_path, monkeypatch):
    r = client(tmp_path, monkeypatch).post(
        "/api/run", json={"text": "Our dashboard is down, 500 error."}
    )
    body = r.json()
    assert r.status_code == 200
    assert body["triage"]["category"] == "outage"
    assert body["trace"]["trace_id"] and body["trace"]["steps"][0] == "triage"
    assert body["trace"]["input_tokens"] > 0


def test_low_confidence_run_enqueues_and_review_flow(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    before = len(c.get("/api/review").json())
    out = c.post("/api/run", json={"text": "What are your office hours?"}).json()
    assert out["needs_review"] and out["review_id"]
    assert len(c.get("/api/review").json()) == before + 1
    assert c.post(f"/api/review/{out['review_id']}/approve").json()["status"] == "approved"
    assert c.post(f"/api/review/{out['review_id']}/reject").status_code == 409
    assert c.post("/api/review/9999/approve").status_code == 404
    assert c.post("/api/review/reset").json()["items"] == 4


def test_eval_endpoint(tmp_path, monkeypatch):
    body = client(tmp_path, monkeypatch).get("/api/eval").json()
    assert body["ok"] and body["total"] == 12
