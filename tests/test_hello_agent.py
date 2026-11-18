from examples.hello_agent.agent import run
from examples.hello_agent.demo import main as demo_main
from fastapi.testclient import TestClient

from fde_kit.config import Settings
from fde_kit.review_queue import ReviewQueue
from fde_kit.server import create_app


def test_confident_ticket_is_auto_routed():
    out = run({"text": "The site is down with a 500 error"})
    assert out["category"] == "outage" and out["priority"] == "p1"
    assert out["needs_review"] is False


def test_unsure_ticket_goes_to_human_queue():
    q = ReviewQueue("sqlite://")
    out = run({"text": "What are your office hours?"}, queue=q)
    assert out["needs_review"] and out["action"] == "queued_for_review"
    item = q.get(out["review_id"])
    assert item["kind"] == "ticket_triage" and item["status"] == "pending"


def test_demo_runs(capsys):
    assert demo_main() == 0
    assert "pending human reviews: 1" in capsys.readouterr().out


def test_server_wiring(tmp_path):
    app = create_app(Settings(review_db_url=f"sqlite:///{tmp_path / 'r.db'}"))
    c = TestClient(app)
    assert c.get("/healthz").json()["provider"] == "fake"
    assert c.post("/webhook", json={"text": "hi"}).status_code == 202
    assert c.get("/review/items").json() == []
