# Customer onboarding checklist

Work top to bottom. Each section ends with an exit criterion; do not start the next section
without it.

## 1. Discovery questions

- [ ] What decision or task is the agent replacing or speeding up? Who does it today, how often, how long does it take?
- [ ] What does a correct outcome look like? Who decides, and can we get 50+ labeled past examples?
- [ ] What is the cost of a wrong answer (money, safety, reputation)? Which mistakes are unacceptable?
- [ ] Which systems hold the inputs (database, mailbox, files, webhooks)? Who owns each one?
- [ ] What happens downstream with the agent's output? Is there a human in the loop today?
- [ ] Volume: peak and average items per day; latency expectations.
- [ ] Existing automation, vendors, or prior failed attempts?

Exit: one-paragraph problem statement and a named customer owner agreed in writing.

## 2. Data access

- [ ] Read-only credentials per source (DB role with `SELECT` only, mailbox app password, drop-folder path, webhook secret).
- [ ] Network path confirmed (VPN, allow-list, private link) and tested from the deployment target.
- [ ] Sample data received: de-identified where possible, with a data dictionary.
- [ ] Golden set built from real past cases (`examples/hello_agent/golden.jsonl` format) and signed off by the customer's domain expert.
- [ ] Retention and deletion rules for anything the kit stores (review queue, logs, traces).

Exit: every connector's `health()` is green in the target environment.

## 3. Security review

- [ ] Data classification and whether it may leave the customer boundary (model provider, tracing vendor).
- [ ] Model provider terms: zero-retention or equivalent, region, sub-processors.
- [ ] Secrets in the customer's secret store; none in images, repos or `.env` files that are committed.
- [ ] Least-privilege IAM / DB roles; SQL connector rejects non-SELECT, but the role must enforce it too.
- [ ] Webhook signing on; review API behind `FDE_REVIEW_API_KEY` or the customer's SSO proxy.
- [ ] Logging reviewed: no payloads or PII in logs; tracing off unless approved.
- [ ] Penetration-test or architecture review ticket opened and cleared.

Exit: written security approval.

## 4. Success metrics

- [ ] Primary metric and target (for example: triage accuracy >= 90% on the golden set).
- [ ] Guardrail metrics: human-review rate, p95 latency, cost per item, error rate.
- [ ] Eval gate wired into CI with the agreed threshold (`fde-kit eval --threshold`).
- [ ] Baseline measured (human or current system) so improvement is defensible.
- [ ] Review cadence set (weekly during pilot).

Exit: metrics dashboard or report the customer can read without us.

## 5. Pilot

- [ ] Shadow mode first: agent runs, output is compared to human decisions, nothing is acted on.
- [ ] Review queue staffed; reviewers trained on approve/reject and what to write in the note.
- [ ] Disagreements fed back into the golden set.
- [ ] Cost and latency measured on real traffic, not synthetic.

Exit: shadow-mode accuracy meets the target for two consecutive weeks.

## 6. Go-live

- [ ] Runbook (`docs/RUNBOOK.md`) reviewed with the customer's on-call team.
- [ ] Alerts: error rate, review-queue depth, cost per day, connector health.
- [ ] Rollback tested (previous image, `LLM_PROVIDER=fake` kill switch).
- [ ] Change window and communication plan agreed.
- [ ] Cutover: enable actions in stages (low-risk category first), watch the dashboard.
- [ ] Handover: customer team can add a golden case, run the eval, and read a trace without us.

Exit: 30 days at target metrics, then move to steady-state support.
