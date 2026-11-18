# Runbook

Operating the fde-kit service in a customer environment. Everything here is generic; replace
hostnames and ticket links with the customer's.

## Service map

| Piece | Where | Health |
|---|---|---|
| App (webhook intake + review API) | container `app`, port 8000 | `GET /healthz` |
| Review queue DB | Postgres (`REVIEW_DB_URL`) or SQLite file | `SELECT count(*) FROM review_items` |
| Model provider | `LLM_PROVIDER` / `LLM_MODEL` | `/healthz` shows provider and model |
| Tracing (optional) | Langfuse / LangSmith, on only if env vars are set | `/healthz` `tracing` list |

## Everyday operations

- **Deploy**: build the image, push it, roll the service (`docker compose up -d --build`, or
  update the ECS service image in `deploy/terraform`). Roll back by redeploying the previous tag.
- **Config change**: all config is env vars (see `.env.example`). Restart after changing.
- **Rotate a secret**: update it in the secret store, restart the app. Rotate `FDE_WEBHOOK_SECRET`
  together with the sender.
- **Switch model or provider**: change `LLM_PROVIDER`/`LLM_MODEL`, run the eval gate against the
  customer golden set, and only then roll out.

## Before any change reaches production

```bash
uv run ruff check . && uv run pytest -q
uv run fde-kit eval --golden <customer-golden>.jsonl --threshold 0.9
```

The threshold is a contract with the customer. Do not lower it to ship; fix the agent or agree a
new threshold in writing.

## Incidents

Find the run first. Every log line is JSON with a `trace_id`; every run logs `run.start` and
`run.end` with latency, tokens and cost.

```bash
docker compose logs app | grep '"trace_id": "<id>"'
```

| Symptom | Likely cause | Action |
|---|---|---|
| Webhook returns 401 | Signature mismatch or wrong `FDE_WEBHOOK_SECRET` | Compare `sha256=` HMAC of the raw body; check clock/proxy body rewriting |
| `structured_output.retry` warnings spike | Model drift or prompt/schema change | Run the eval gate; pin model version; raise `LLM_MAX_RETRIES` only as a stopgap |
| Run errors with `LLMConfigError` | Missing key or extra | Check `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is mounted |
| Review queue growing | Reviewers away, or confidence threshold too high | Check pending count; page the reviewer group; tune the review threshold with eval data |
| Cost per run jumps | Longer inputs or more retries | Compare `input_tokens`, `output_tokens` in `run.end` logs; check retry rate |
| Connector returns nothing | Upstream credentials expired or filter changed | Call the connector's `health()`; check `FDE_SQL_*`, `FDE_IMAP_*`, `FDE_DROP_*` |

## Kill switch

Set `LLM_PROVIDER=fake` (deterministic keyword triage) or scale the service to zero. Intake keeps
buffering at the sender if it retries; the review queue is unaffected.

## Data handling

- The review queue stores the payload the agent saw. Treat it like the source system: same
  retention, same access controls.
- Logs contain trace ids, token counts and cost, not payloads, unless a caller adds them via
  `extra=`. Review that before enabling Langfuse/LangSmith, which do capture prompts.

## Escalation

1. On-call engineer (first responder).
2. Customer technical owner (named during onboarding).
3. Customer security contact for suspected data exposure.
