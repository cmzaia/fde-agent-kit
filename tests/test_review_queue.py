import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_kit.review_queue import AlreadyDecidedError, ReviewQueue, create_review_router


def test_enqueue_list_decide():
    q = ReviewQueue("sqlite://")
    a = q.enqueue("k", {"x": 1}, trace_id="t1")
    q.enqueue("k", {"x": 2})
    assert [i["status"] for i in q.list()] == ["pending", "pending"]
    done = q.decide(a, "approved", reviewer="christian", note="ok")
    assert done["status"] == "approved" and done["decided_at"] and done["trace_id"] == "t1"
    assert len(q.list(status="pending")) == 1


def test_double_decision_blocked():
    q = ReviewQueue("sqlite://")
    i = q.enqueue("k", {})
    q.decide(i, "rejected")
    with pytest.raises(AlreadyDecidedError):
        q.decide(i, "approved")
    with pytest.raises(KeyError):
        q.decide(999, "approved")


def test_file_backed_persists(tmp_path):
    url = f"sqlite:///{tmp_path / 'q.db'}"
    ReviewQueue(url).enqueue("k", {"a": 1})
    assert ReviewQueue(url).list()[0]["payload"] == {"a": 1}


def make_client(api_key=None):
    app = FastAPI()
    app.include_router(create_review_router(ReviewQueue("sqlite://"), api_key), prefix="/review")
    return TestClient(app)


def test_api_flow():
    c = make_client()
    created = c.post("/review/items", json={"kind": "triage", "payload": {"t": "x"}}).json()
    assert created["status"] == "pending"
    assert len(c.get("/review/items", params={"status": "pending"}).json()) == 1
    r = c.post(f"/review/items/{created['id']}/approve", json={"reviewer": "me"})
    assert r.json()["status"] == "approved" and r.json()["reviewer"] == "me"
    assert c.post(f"/review/items/{created['id']}/reject").status_code == 409
    assert c.get("/review/items/999").status_code == 404
    assert c.post("/review/items/999/approve").status_code == 404


def test_api_reject_without_body():
    c = make_client()
    i = c.post("/review/items", json={"kind": "k", "payload": {}}).json()["id"]
    assert c.post(f"/review/items/{i}/reject").json()["status"] == "rejected"


def test_api_key_required_when_set():
    c = make_client("k")
    assert c.get("/review/items").status_code == 401
    assert c.get("/review/items", headers={"X-API-Key": "k"}).status_code == 200
