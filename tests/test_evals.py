import json

import pytest

from fde_kit.cli import main
from fde_kit.evals import (
    GoldenCase,
    LLMJudge,
    check_deterministic,
    load_golden,
    percentile,
    run_eval,
)
from fde_kit.llm import FakeChatModel
from fde_kit.observability import record_llm_usage

GOLDEN = "examples/hello_agent/golden.jsonl"


def case(i, expected, text="x"):
    return GoldenCase(id=i, input={"text": text}, expected=expected)


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert percentile([], 50) == 0.0


def test_check_operators():
    c = case("a", {"cat": {"$in": ["x", "y"]}, "s": {"$contains": "REF"}, "n": 3})
    assert check_deterministic(c, {"cat": "Y", "s": "a ref b", "n": 3}) == []
    assert len(check_deterministic(c, {"cat": "z", "s": "no", "n": 4})) == 3


def test_load_golden_errors(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text("# comment\n\n")
    with pytest.raises(ValueError, match="no cases"):
        load_golden(p)
    p.write_text("{bad json}\n")
    with pytest.raises(ValueError, match=":1:"):
        load_golden(p)


def test_load_shipped_golden():
    assert len(load_golden(GOLDEN)) == 12


def test_run_eval_accuracy_and_costs():
    def agent(inp):
        record_llm_usage("fake-triage-1", {"input_tokens": 1000, "output_tokens": 100})
        return {"v": inp["text"]}

    cases = [case("ok", {"v": "a"}, "a"), case("bad", {"v": "a"}, "b")]
    rep = run_eval(agent, cases, threshold=0.5)
    assert rep.accuracy == 0.5 and rep.ok and rep.cost_usd > 0
    assert rep.input_tokens == 2000
    assert not rep.results[1].passed and "expected 'a'" in rep.results[1].mismatches[0]
    assert "| Accuracy | 50.0% (1/2) |" in rep.to_markdown()
    assert json.loads(rep.to_json())["total"] == 2


def test_agent_exception_is_a_failed_case():
    def agent(_):
        raise RuntimeError("boom")

    rep = run_eval(agent, [case("c", {"v": 1})], threshold=1.0)
    assert not rep.ok and "boom" in rep.results[0].error


def test_llm_judge_gates_pass():
    good = run_eval(lambda i: {"v": 1}, [case("c", {"v": 1})], judge=LLMJudge(FakeChatModel()))
    assert good.results[0].judge_score == 1.0 and good.results[0].passed
    # deterministic check passes, but a low judge score still fails the case
    strict = run_eval(lambda i: {"v": 1}, [case("c", {"v": 1})], judge=lambda c, a: (0.2, "meh"))
    assert strict.results[0].judge_score == 0.2 and not strict.results[0].passed


def test_cli_passes_above_threshold(capsys):
    assert main(["eval", "--golden", GOLDEN, "--threshold", "0.9"]) == 0
    assert "Accuracy" in capsys.readouterr().out


def test_cli_fails_below_threshold(capsys, tmp_path):
    out = tmp_path / "r.json"
    assert main(["eval", "--golden", GOLDEN, "--threshold", "0.99", "--json-out", str(out)]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert json.loads(out.read_text())["passed"] == 11


def test_cli_judge_flag(capsys):
    assert main(["eval", "--golden", GOLDEN, "--threshold", "0.9", "--judge"]) == 0
