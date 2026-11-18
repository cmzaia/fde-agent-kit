import csv
import hashlib
import hmac
import json
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fde_kit.connectors import (
    DropFolderConnector,
    IMAPConnector,
    SQLConnector,
    WebhookConnector,
    available_connectors,
    build_connector,
)
from fde_kit.connectors.dropfolder import DropFolderSettings
from fde_kit.connectors.imap import IMAPSettings
from fde_kit.connectors.sql import SQLSettings, validate_readonly
from fde_kit.connectors.webhook import WebhookSettings, create_webhook_router, sign


# ---- registry
def test_registry_lists_all_four():
    assert available_connectors() == ["dropfolder", "imap", "sql", "webhook"]


def test_build_from_env(monkeypatch):
    monkeypatch.setenv("FDE_SQL_QUERY", "SELECT 42 AS answer")
    recs = list(build_connector("sql").fetch())
    assert recs[0].payload == {"answer": 42}


def test_build_unknown():
    with pytest.raises(KeyError):
        build_connector("nope")


# ---- sql
@pytest.fixture
def engine():
    e = create_engine("sqlite://", poolclass=StaticPool)
    with e.begin() as c:
        c.execute(text("CREATE TABLE t (id INTEGER, name TEXT)"))
        c.execute(text("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c')"))
    return e


def test_sql_fetch_with_id_column(engine):
    c = SQLConnector(
        SQLSettings(query="SELECT * FROM t ORDER BY id", id_column="id"), engine=engine
    )
    recs = list(c.fetch())
    assert [r.id for r in recs] == ["1", "2", "3"] and recs[0].payload["name"] == "a"
    assert c.health().ok


def test_sql_max_rows(engine):
    c = SQLConnector(SQLSettings(query="SELECT * FROM t", max_rows=2), engine=engine)
    assert len(list(c.fetch())) == 2


@pytest.mark.parametrize(
    "q", ["DELETE FROM t", "SELECT 1; DROP TABLE t", "WITH x AS (SELECT 1) DELETE FROM t"]
)
def test_sql_rejects_writes(q):
    with pytest.raises(ValueError):
        validate_readonly(q)


def test_sql_accepts_cte():
    assert validate_readonly("WITH x AS (SELECT 1 a) SELECT * FROM x;").startswith("WITH")


# ---- imap (fake client)
def make_mail(subject="Invoice question", body="Why was I charged?"):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Message-ID"] = (
        subject,
        "a@x.test",
        "support@y.test",
        "<1@x.test>",
    )
    m.set_content(body)
    m.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="inv.bin")
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages):
        self.messages, self.events = messages, []

    def login(self, u, p):
        self.events.append(("login", u, p))

    def select(self, mailbox, readonly=False):
        self.events.append(("select", mailbox, readonly))

    def search(self, charset, criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, mid, what):
        assert "PEEK" in what
        return "OK", [(b"1 (BODY[] {n}", self.messages[int(mid) - 1]), b")"]

    def logout(self):
        self.events.append(("logout",))


def test_imap_reads_and_logs_out():
    fake = FakeIMAP([make_mail(), make_mail("Second", "Hello")])
    c = IMAPConnector(IMAPSettings(username="u", password="pw"), client_factory=lambda: fake)
    recs = list(c.fetch())
    assert len(recs) == 2
    assert recs[0].payload["subject"] == "Invoice question"
    assert recs[0].payload["body"] == "Why was I charged?"
    assert recs[0].payload["attachments"] == ["inv.bin"]
    assert ("select", "INBOX", True) in fake.events  # read-only
    assert fake.events[-1] == ("logout",)


def test_imap_max_messages():
    fake = FakeIMAP([make_mail()] * 5)
    c = IMAPConnector(IMAPSettings(max_messages=2), client_factory=lambda: fake)
    assert len(list(c.fetch())) == 2


def test_imap_health_false_on_error():
    def boom():
        raise OSError("no route")

    assert not IMAPConnector(IMAPSettings(), client_factory=boom).health().ok


# ---- dropfolder
def make_pdf(text: str) -> bytes:
    """Smallest valid one-page PDF with a correct xref table."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_dropfolder_csv_and_pdf_then_archives(tmp_path):
    with (tmp_path / "orders.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerows([["sku", "qty"], ["A1", "2"], ["B2", "5"]])
    (tmp_path / "doc.pdf").write_bytes(make_pdf("Hello PDF"))
    (tmp_path / "ignore.txt").write_text("x")
    c = DropFolderConnector(DropFolderSettings(path=tmp_path))
    recs = list(c.fetch())
    ids = sorted(r.id for r in recs)
    assert ids == ["doc.pdf", "orders.csv:1", "orders.csv:2"]
    pdf = next(r for r in recs if r.id == "doc.pdf")
    assert "Hello PDF" in pdf.payload["text"] and pdf.payload["pages"] == 1
    assert (tmp_path / "processed" / "orders.csv").exists()
    assert (tmp_path / "ignore.txt").exists()
    assert list(c.fetch()) == []  # nothing new


def test_dropfolder_watch_handles_files_across_cycles(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n")
    seen = []
    DropFolderConnector(DropFolderSettings(path=tmp_path)).watch(
        seen.append, interval=0, max_cycles=2
    )
    assert len(seen) == 1


# ---- webhook
def client_for(secret=None):
    conn = WebhookConnector(WebhookSettings(secret=secret))
    app = FastAPI()
    app.include_router(create_webhook_router(conn), prefix="/webhook")
    return conn, TestClient(app)


def test_webhook_unsigned_accepts_and_drains():
    conn, client = client_for()
    r = client.post("/webhook", json={"a": 1}, headers={"X-Event-Id": "e1"})
    assert r.status_code == 202 and r.json() == {"id": "e1"}
    assert [x.payload for x in conn.fetch()] == [{"a": 1}]
    assert list(conn.fetch()) == []


def test_webhook_signature_enforced():
    conn, client = client_for("s3cret")
    body = json.dumps({"a": 1}).encode()
    assert client.post("/webhook", content=body).status_code == 401
    assert (
        client.post("/webhook", content=body, headers={"X-Signature": "sha256=bad"}).status_code
        == 401
    )
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert good == sign("s3cret", body)
    assert client.post("/webhook", content=body, headers={"X-Signature": good}).status_code == 202


def test_webhook_rejects_non_object():
    _, client = client_for()
    assert client.post("/webhook", content=b"[1]").status_code == 400
    assert client.post("/webhook", content=b"nope").status_code == 400
