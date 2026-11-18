"""Public demo dashboard: a single server-rendered page over the kit, running on the fake model.

Stateless-host friendly: the review queue lives in SQLite under the temp dir and is re-seeded
whenever it is empty, and the eval report is computed on first use and cached in memory.
"""

from __future__ import annotations

import os
import tempfile
import threading
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

os.environ.setdefault("LLM_PROVIDER", "fake")

from examples.hello_agent.agent import REVIEW_BELOW, build_graph  # noqa: E402

from fde_kit.connectors import available_connectors  # noqa: E402
from fde_kit.evals import EvalReport, load_golden, run_eval  # noqa: E402
from fde_kit.llm import get_llm  # noqa: E402
from fde_kit.observability import trace_run  # noqa: E402
from fde_kit.review_queue import AlreadyDecidedError, ReviewQueue  # noqa: E402
from fde_kit.review_queue.store import APPROVED, REJECTED, review_items  # noqa: E402

REPO_URL = "https://github.com/cmzaia/fde-agent-kit"
GOLDEN = Path(__file__).resolve().parents[2] / "examples" / "hello_agent" / "golden.jsonl"
THRESHOLD = 0.9

SAMPLES = [
    "I was charged twice on my last invoice, please refund one.",
    "Our dashboard is down, every page returns a 500 error.",
    "I forgot my password and I am locked out of my account.",
    "Can you tell me what your office hours are?",
]

SEED_ITEMS = [
    (
        "Hi, does your plan cover on-site installs in Rotterdam? Asking for a colleague.",
        "other",
        "p3",
        0.41,
    ),
    (
        "The numbers on my monthly summary look odd but I am not sure what changed.",
        "billing",
        "p3",
        0.52,
    ),
    ("Some users see a blank page after the update, others are fine.", "outage", "p2", 0.58),
    ("Please add my new teammate to the account, she starts Monday.", "access", "p3", 0.46),
]

CONNECTORS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "sql": (
        "Read rows from any SQLAlchemy database",
        [
            ("FDE_SQL_URL", "sqlite:///:memory:"),
            ("FDE_SQL_QUERY", "SELECT 1 AS ok"),
            ("FDE_SQL_ID_COLUMN", "unset"),
            ("FDE_SQL_MAX_ROWS", "1000"),
        ],
    ),
    "imap": (
        "Pull unread mail from a shared mailbox",
        [
            ("FDE_IMAP_HOST", "localhost"),
            ("FDE_IMAP_PORT", "993"),
            ("FDE_IMAP_USERNAME", ""),
            ("FDE_IMAP_PASSWORD", "secret"),
            ("FDE_IMAP_MAILBOX", "INBOX"),
            ("FDE_IMAP_SEARCH", "UNSEEN"),
            ("FDE_IMAP_MAX_MESSAGES", "50"),
        ],
    ),
    "dropfolder": (
        "Watch a folder for CSV, text and PDF drops",
        [("FDE_DROP_PATH", "./drop"), ("FDE_DROP_PROCESSED_DIR", "processed")],
    ),
    "webhook": (
        "Receive HMAC-signed vendor webhooks",
        [("FDE_WEBHOOK_SECRET", "unset"), ("FDE_WEBHOOK_MAX_QUEUE", "10000")],
    ),
}


class RunRequest(BaseModel):
    text: str | None = None


class DemoState:
    def __init__(self) -> None:
        db = os.environ.get("DEMO_DB_PATH") or os.path.join(tempfile.gettempdir(), "fde_demo.db")
        self.queue = ReviewQueue(f"sqlite:///{db}")
        self.lock = threading.Lock()
        self._report: EvalReport | None = None
        self.seed()

    def seed(self) -> None:
        with self.lock:
            if self.queue.list(limit=1):
                return
            for i, (text, cat, prio, conf) in enumerate(SEED_ITEMS):
                self.queue.enqueue(
                    "ticket_triage",
                    {
                        "text": text,
                        "category": cat,
                        "priority": prio,
                        "confidence": conf,
                        "summary": text[:80],
                    },
                    f"seed{i:012x}",
                )

    def report(self, refresh: bool = False) -> EvalReport:
        if self._report is None or refresh:
            from examples.hello_agent.agent import run

            llm = get_llm()
            self._report = run_eval(
                lambda inp: run(inp, llm=llm), load_golden(GOLDEN), threshold=THRESHOLD
            )
        return self._report


def run_agent(state: DemoState, text: str) -> dict[str, Any]:
    llm = get_llm()
    graph = build_graph(llm, state.queue)
    steps: list[str] = []
    final: dict[str, Any] = {}
    with trace_run("demo:hello_agent") as ctx:
        for update in graph.stream({"text": text}, stream_mode="updates"):
            for node, out in update.items():
                steps.append(node)
                final.update(out)
    u = ctx.usage
    return {
        "input": text,
        "triage": final.get("triage", {}),
        "action": final.get("action"),
        "needs_review": final.get("needs_review"),
        "review_id": final.get("review_id"),
        "trace": {
            "trace_id": ctx.trace_id,
            "steps": steps,
            "llm_calls": u.calls,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": round(u.cost_usd, 6),
            "latency_ms": round(ctx.latency_s * 1000, 2),
        },
    }


# --------------------------------------------------------------------------- rendering


def render_eval(r: EvalReport) -> str:
    verdict = "PASS" if r.ok else "FAIL"
    cls = "pass" if r.ok else "fail"
    stats = [
        ("Accuracy", f"{r.accuracy:.1%}", f"{r.passed}/{r.total} cases"),
        ("Threshold", f"{r.threshold:.0%}", f'<span class="badge {cls}">{verdict}</span>'),
        ("Latency p50", f"{r.p50_latency_s * 1000:.1f} ms", ""),
        ("Latency p95", f"{r.p95_latency_s * 1000:.1f} ms", ""),
        ("Est. cost", f"${r.cost_usd:.4f}", f"{r.input_tokens} in / {r.output_tokens} out tokens"),
    ]
    tiles = "".join(
        f'<div class="stat"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="s">{s}</div></div>'
        for k, v, s in stats
    )
    rows = "".join(
        f"<tr><td><code>{escape(c.id)}</code></td>"
        f'<td><span class="badge {"pass" if c.passed else "fail"}">'
        f"{'pass' if c.passed else 'fail'}</span></td>"
        f"<td>{escape(str((c.actual or {}).get('category', '-')))}/"
        f"{escape(str((c.actual or {}).get('priority', '-')))}</td>"
        f'<td class="num">{c.latency_s * 1000:.1f} ms</td>'
        f'<td class="num">{c.input_tokens + c.output_tokens}</td>'
        f'<td class="num">${c.cost_usd:.6f}</td>'
        f"<td>{escape(c.error or '; '.join(c.mismatches))}</td></tr>"
        for c in r.results
    )
    return (
        f'<div class="stats">{tiles}</div>'
        '<div class="scroll"><table id="eval-table"><thead><tr><th>Case</th><th>Result</th>'
        '<th>Predicted</th><th class="num">Latency</th><th class="num">Tokens</th>'
        f'<th class="num">Cost</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def render_review(state: DemoState) -> str:
    items = state.queue.list(limit=50)
    if not items:
        return '<p class="muted">Queue is empty.</p>'
    out = []
    for it in reversed(items):
        p = it["payload"]
        status = it["status"]
        if status == "pending":
            actions = (
                f'<button class="btn ok" data-review="approve" data-id="{it["id"]}">Approve</button>'
                f'<button class="btn no" data-review="reject" data-id="{it["id"]}">Reject</button>'
            )
        else:
            actions = ""
        out.append(
            f'<li class="item"><div class="item-main"><div class="item-text">{escape(p.get("text", ""))}</div>'
            f'<div class="meta"><code>#{it["id"]}</code> '
            f"{escape(str(p.get('category', '')))}/{escape(str(p.get('priority', '')))} "
            f"&middot; confidence {float(p.get('confidence', 0)):.2f} "
            f"&middot; trace <code>{escape(str(it['trace_id'] or '-'))}</code></div></div>"
            f'<div class="item-side"><span class="badge {status}">{status}</span>{actions}</div></li>'
        )
    return f'<ul class="items">{"".join(out)}</ul>'


def render_connectors() -> str:
    names = available_connectors()
    cards = []
    for n in names:
        desc, env = CONNECTORS.get(n, ("", []))
        rows = "".join(
            f"<tr><td><code>{escape(k)}</code></td><td><code>{escape(v) or '&nbsp;'}</code></td></tr>"
            for k, v in env
        )
        cards.append(
            f'<div class="card conn"><h3>{escape(n)}</h3><p class="muted">{escape(desc)}</p>'
            f'<table class="env"><thead><tr><th>Env var</th><th>Default</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    return f'<div class="grid">{"".join(cards)}</div>'


CSS = """
:root{--bg:#f6f7f9;--fg:#14171f;--muted:#5c6475;--card:#fff;--line:#e3e6ec;--accent:#3b4cca;
--ok:#0f7b4a;--okbg:#e2f5ec;--bad:#b3261e;--badbg:#fdeceb;--warn:#8a5a00;--warnbg:#fff3d6;
--code:#eef0f5}
@media (prefers-color-scheme:dark){:root{--bg:#0e1118;--fg:#e8eaf0;--muted:#9aa3b5;--card:#161a23;
--line:#262b38;--accent:#8c9bff;--ok:#5fd39a;--okbg:#12301f;--bad:#ff8a80;--badbg:#3a1714;
--warn:#f0c26a;--warnbg:#33290f;--code:#1f2430}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px 48px}
header.hero{padding:40px 0 24px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);
font-weight:600}
h1{font-size:clamp(26px,5vw,38px);line-height:1.15;margin:6px 0 10px}
.lead{max-width:720px;color:var(--muted);font-size:16.5px;margin:0 0 16px}
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:3px 12px;
font-size:13px;color:var(--muted)}
section{margin-top:28px}
h2{font-size:18px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 12px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.muted{color:var(--muted)}
code{background:var(--code);padding:1px 5px;border-radius:5px;font:12.5px ui-monospace,
SFMono-Regular,Menlo,monospace;word-break:break-all}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch}
select,input[type=text]{font:inherit;color:var(--fg);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:9px 10px;min-width:0}
input[type=text]{flex:1 1 260px}
.btn{font:inherit;font-weight:600;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--fg);padding:9px 16px;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.ok{color:var(--ok);border-color:var(--ok);padding:5px 12px}
.btn.no{color:var(--bad);border-color:var(--bad);padding:5px 12px}
.btn:disabled{opacity:.55;cursor:wait}
#run-out{margin-top:14px}
.result{display:grid;gap:12px;grid-template-columns:1fr 1fr}
@media(max-width:700px){.result{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0}
.kv dt{color:var(--muted)}.kv dd{margin:0;word-break:break-word}
h3{font-size:14px;margin:0 0 8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
margin-bottom:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.stat .v{font-size:24px;font-weight:700;margin-top:2px}.stat .s{font-size:12.5px;color:var(--muted)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.num{text-align:right}
.badge{display:inline-block;border-radius:999px;padding:1px 10px;font-size:12px;font-weight:600}
.badge.pass,.badge.approved{background:var(--okbg);color:var(--ok)}
.badge.fail,.badge.rejected{background:var(--badbg);color:var(--bad)}
.badge.pending{background:var(--warnbg);color:var(--warn)}
ul.items{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.item{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap;
background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.item-main{flex:1 1 320px;min-width:0}.item-text{font-weight:500}
.meta{font-size:12.5px;color:var(--muted);margin-top:3px}
.item-side{display:flex;gap:8px;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.conn p{margin:0 0 10px;font-size:14px}
table.env td,table.env th{padding:5px 0;border-bottom:1px solid var(--line);white-space:normal}
table.env th:last-child,table.env td:last-child{padding-left:10px}
.head-row{display:flex;justify-content:space-between;align-items:end;gap:10px;flex-wrap:wrap}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);
font-size:14px;display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}
a{color:var(--accent)}
.err{color:var(--bad)}
"""

JS = """
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function swap(url,sel){const r=await fetch(url);$(sel).innerHTML=await r.text();}
$('#sample').addEventListener('change',e=>{$('#ticket').value=e.target.value;});
$('#run').addEventListener('click',async()=>{
  const b=$('#run');b.disabled=true;b.textContent='Running...';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({text:$('#ticket').value})});
    if(!r.ok)throw new Error(await r.text());
    const d=await r.json(),t=d.trace,tr=d.triage;
    $('#run-out').innerHTML=`<div class="result">
      <div class="card"><h3>Agent output</h3><dl class="kv">
      <dt>Category</dt><dd>${esc(tr.category)}</dd><dt>Priority</dt><dd>${esc(tr.priority)}</dd>
      <dt>Confidence</dt><dd>${Number(tr.confidence).toFixed(2)}</dd>
      <dt>Action</dt><dd><span class="badge ${d.needs_review?'pending':'pass'}">${esc(d.action)}</span></dd>
      <dt>Summary</dt><dd>${esc(tr.summary)}</dd></dl></div>
      <div class="card"><h3>Trace</h3><dl class="kv">
      <dt>Trace id</dt><dd><code>${esc(t.trace_id)}</code></dd>
      <dt>Steps</dt><dd>${t.steps.map(esc).join(' &rarr; ')}</dd>
      <dt>Tokens</dt><dd>${t.input_tokens} in / ${t.output_tokens} out (${t.llm_calls} call)</dd>
      <dt>Est. cost</dt><dd>$${t.cost_usd.toFixed(6)}</dd>
      <dt>Latency</dt><dd>${t.latency_ms} ms</dd></dl></div></div>`;
    if(d.needs_review)swap('/fragments/review','#review');
  }catch(e){$('#run-out').innerHTML='<p class="err">Run failed: '+esc(e.message)+'</p>';}
  b.disabled=false;b.textContent='Run the hello agent';
});
$('#rerun-eval').addEventListener('click',async()=>{
  const b=$('#rerun-eval');b.disabled=true;
  await fetch('/api/eval?refresh=true');await swap('/fragments/eval','#eval');b.disabled=false;
});
$('#review').addEventListener('click',async e=>{
  const b=e.target.closest('[data-review]');if(!b)return;b.disabled=true;
  await fetch(`/api/review/${b.dataset.id}/${b.dataset.review}`,{method:'POST'});
  await swap('/fragments/review','#review');
});
$('#reset').addEventListener('click',async()=>{
  await fetch('/api/review/reset',{method:'POST'});await swap('/fragments/review','#review');
});
"""


def render_page(state: DemoState) -> str:
    options = "".join(
        f'<option value="{escape(s, quote=True)}">{escape(s)}</option>' for s in SAMPLES
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fde-agent-kit: live demo</title>
<meta name="description" content="Connectors, an eval gate, traces with cost accounting and a human review queue for deploying LLM agents into customer environments.">
<meta property="og:type" content="website">
<meta property="og:title" content="fde-agent-kit: live demo">
<meta property="og:description" content="Connectors, an eval gate, traces with cost accounting and a human review queue for deploying LLM agents into customer environments.">
<meta property="og:url" content="https://fde-agent-kit.vercel.app">
<meta property="og:image" content="https://raw.githubusercontent.com/cmzaia/fde-agent-kit/main/docs/screenshots/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://raw.githubusercontent.com/cmzaia/fde-agent-kit/main/docs/screenshots/og.png">
<meta name="description" content="Connectors, evals, observability and human review for LLM agents in customer environments.">
<style>{CSS}</style></head><body><div class="wrap">
<header class="hero">
<div class="eyebrow">Live demo &middot; runs offline on a deterministic fake model</div>
<h1>fde-agent-kit</h1>
<p class="lead">The scaffolding I want on day one of a forward-deployed engagement: connectors into
whatever the customer already runs, an eval gate that says whether the agent is good enough to ship,
traces with token and cost accounting, and a human review queue for the calls the agent should not
make alone.</p>
<div class="pills"><span class="pill">Connectors</span><span class="pill">Eval gate</span>
<span class="pill">Observability</span><span class="pill">Review queue</span>
<span class="pill">LangGraph</span><span class="pill">FastAPI</span></div>
</header>

<section><h2>Run the hello agent</h2>
<p class="sub">Support-ticket triage as a small LangGraph. Confidence below {REVIEW_BELOW:.1f}
goes to the review queue below.</p>
<div class="card"><div class="row">
<select id="sample" aria-label="Sample ticket"><option value="">Pick a sample...</option>{options}</select>
<input type="text" id="ticket" aria-label="Ticket text" value="{escape(SAMPLES[1], quote=True)}">
<button class="btn primary" id="run">Run the hello agent</button></div>
<div id="run-out"><p class="muted" style="margin:12px 0 0">Press the button to see output and trace.</p></div>
</div></section>

<section><div class="head-row"><div><h2>Latest eval report</h2>
<p class="sub">{len(load_golden(GOLDEN))} golden cases, fake provider, gate at {THRESHOLD:.0%} accuracy.</p></div>
<button class="btn" id="rerun-eval">Re-run eval</button></div>
<div id="eval">{render_eval(state.report())}</div></section>

<section><div class="head-row"><div><h2>Review queue</h2>
<p class="sub">Human-in-the-loop decisions, stored in SQLite.</p></div>
<button class="btn" id="reset">Reset demo data</button></div>
<div id="review">{render_review(state)}</div></section>

<section><h2>Connectors</h2>
<p class="sub">Each one is configured entirely through environment variables.</p>
{render_connectors()}</section>

<footer><span>Demo data resets periodically.</span>
<span><a href="{REPO_URL}">View source on GitHub</a></span></footer>
</div><script>{JS}</script></body></html>"""


# --------------------------------------------------------------------------- app


def create_demo_app(state: DemoState | None = None) -> FastAPI:
    st = state or DemoState()
    app = FastAPI(title="fde-agent-kit demo", docs_url=None, redoc_url=None)
    app.state.demo = st

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        st.seed()
        return render_page(st)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "provider": os.environ.get("LLM_PROVIDER", "fake")}

    @app.post("/api/run")
    def api_run(req: RunRequest | None = None) -> dict[str, Any]:
        text = ((req.text if req else None) or SAMPLES[1]).strip()[:2000]
        if not text:
            raise HTTPException(422, "text is empty")
        return run_agent(st, text)

    @app.get("/api/eval")
    def api_eval(refresh: bool = False) -> dict[str, Any]:
        r = st.report(refresh)
        return {**r.model_dump(), "ok": r.ok}

    @app.get("/fragments/eval", response_class=HTMLResponse)
    def frag_eval() -> str:
        return render_eval(st.report())

    @app.get("/fragments/review", response_class=HTMLResponse)
    def frag_review() -> str:
        st.seed()
        return render_review(st)

    @app.get("/api/review")
    def api_review() -> list[dict[str, Any]]:
        st.seed()
        return st.queue.list()

    def decide(item_id: int, status: str) -> dict[str, Any]:
        try:
            return st.queue.decide(item_id, status, reviewer="demo visitor")
        except KeyError:
            raise HTTPException(404, "no such item") from None
        except AlreadyDecidedError:
            raise HTTPException(409, "item already decided") from None

    @app.post("/api/review/reset")
    def reset() -> dict[str, int]:
        from sqlalchemy import delete

        with st.queue.engine.begin() as conn:
            conn.execute(delete(review_items))
        st.seed()
        return {"items": len(st.queue.list())}

    @app.post("/api/review/{item_id}/approve")
    def approve(item_id: int) -> dict[str, Any]:
        return decide(item_id, APPROVED)

    @app.post("/api/review/{item_id}/reject")
    def reject(item_id: int) -> dict[str, Any]:
        return decide(item_id, REJECTED)

    return app
