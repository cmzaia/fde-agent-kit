"""Eval harness: run an agent over a JSONL golden set; deterministic checks + optional LLM judge."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from fde_kit.llm import structured_output
from fde_kit.observability import trace_run

log = logging.getLogger("fde_kit.evals")

Agent = Callable[[dict[str, Any]], dict[str, Any]]
Judge = Callable[["GoldenCase", dict[str, Any]], tuple[float, str]]


class GoldenCase(BaseModel):
    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    id: str
    passed: bool
    mismatches: list[str] = Field(default_factory=list)
    judge_score: float | None = None
    actual: dict[str, Any] | None = None
    error: str | None = None
    latency_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    trace_id: str = ""


class EvalReport(BaseModel):
    total: int
    passed: int
    accuracy: float
    threshold: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    p50_latency_s: float
    p95_latency_s: float
    results: list[CaseResult]

    @property
    def ok(self) -> bool:
        return self.accuracy >= self.threshold

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def to_markdown(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            "| Metric | Value |",
            "|---|---|",
            f"| Accuracy | {self.accuracy:.1%} ({self.passed}/{self.total}) |",
            f"| Threshold | {self.threshold:.1%} ({verdict}) |",
            f"| Est. cost | ${self.cost_usd:.4f} |",
            f"| Tokens in/out | {self.input_tokens} / {self.output_tokens} |",
            f"| Latency p50 | {self.p50_latency_s * 1000:.1f} ms |",
            f"| Latency p95 | {self.p95_latency_s * 1000:.1f} ms |",
            "",
            "| Case | Result | Judge | Latency | Notes |",
            "|---|---|---|---|---|",
        ]
        for r in self.results:
            judge = "-" if r.judge_score is None else f"{r.judge_score:.2f}"
            note = r.error or "; ".join(r.mismatches)
            lines.append(
                f"| {r.id} | {'pass' if r.passed else 'FAIL'} | {judge} "
                f"| {r.latency_s * 1000:.1f} ms | {note} |"
            )
        return "\n".join(lines)


def load_golden(path: str | Path) -> list[GoldenCase]:
    cases = []
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("#"):
            try:
                cases.append(GoldenCase.model_validate_json(line))
            except ValueError as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    if not cases:
        raise ValueError(f"{path}: no cases")
    return cases


def _match(expected: Any, actual: Any) -> bool:
    """Equality, plus {"$in": [...]} and {"$contains": "..."} operators. Strings compare loosely."""
    if isinstance(expected, dict) and len(expected) == 1:
        op, arg = next(iter(expected.items()))
        if op == "$in":
            return any(_match(a, actual) for a in arg)
        if op == "$contains":
            return str(arg).lower() in str(actual).lower()
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def check_deterministic(case: GoldenCase, actual: dict[str, Any]) -> list[str]:
    return [
        f"{k}: expected {v!r}, got {actual.get(k)!r}"
        for k, v in case.expected.items()
        if not _match(v, actual.get(k))
    ]


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (matches numpy's default)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class JudgeVerdict(BaseModel):
    score: float = Field(ge=0, le=1)
    reason: str = ""


class LLMJudge:
    """Optional rubric grader; its score gates a case on top of the deterministic checks."""

    def __init__(self, llm: Any, pass_score: float = 0.5):
        self.llm, self.pass_score = llm, pass_score

    def __call__(self, case: GoldenCase, actual: dict[str, Any]) -> tuple[float, str]:
        prompt = (
            "JUDGE: grade whether ACTUAL satisfies EXPECTED for the given input. "
            "Score 0 to 1.\n"
            f"INPUT: {json.dumps(case.input, sort_keys=True)}\n"
            f"EXPECTED: {json.dumps(case.expected, sort_keys=True)}\n"
            f"ACTUAL: {json.dumps(actual, sort_keys=True, default=str)}"
        )
        v = structured_output(self.llm, JudgeVerdict, prompt, system="You are a strict grader.")
        return v.score, v.reason


def run_eval(
    agent: Agent,
    cases: list[GoldenCase],
    *,
    judge: Judge | None = None,
    judge_pass_score: float = 0.5,
    threshold: float = 0.9,
) -> EvalReport:
    results: list[CaseResult] = []
    for case in cases:
        t0 = time.perf_counter()
        with trace_run(f"eval:{case.id}") as ctx:
            try:
                actual = agent(case.input)
                error = None
            except Exception as e:  # a crashing agent is a failed case, not a crashed eval
                actual, error = None, f"{type(e).__name__}: {e}"
            latency = time.perf_counter() - t0
            mismatches = (
                check_deterministic(case, actual) if actual is not None else ["agent raised"]
            )
            score = None
            if judge and actual is not None:
                try:
                    score, _ = judge(case, actual)
                except Exception as e:
                    error = f"judge failed: {e}"
            passed = (
                not mismatches and error is None and (score is None or score >= judge_pass_score)
            )
        results.append(
            CaseResult(
                id=case.id,
                passed=passed,
                mismatches=mismatches if not passed else [],
                judge_score=score,
                actual=actual,
                error=error,
                latency_s=latency,
                cost_usd=ctx.usage.cost_usd,
                input_tokens=ctx.usage.input_tokens,
                output_tokens=ctx.usage.output_tokens,
                trace_id=ctx.trace_id,
            )
        )
    n_pass = sum(r.passed for r in results)
    lat = [r.latency_s for r in results]
    return EvalReport(
        total=len(results),
        passed=n_pass,
        accuracy=n_pass / len(results) if results else 0.0,
        threshold=threshold,
        cost_usd=sum(r.cost_usd for r in results),
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        p50_latency_s=percentile(lat, 50),
        p95_latency_s=percentile(lat, 95),
        results=results,
    )
