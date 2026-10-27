"""M4 model comparison with a fake client: scoring needs the right label AND a verified quote; budget stops loudly."""
import json
from types import SimpleNamespace as NS

import pytest

from agents import compare, testset


class Fake:
    """Answers right with a real quote on 2.02, wrong label elsewhere; the second run flips one answer."""
    def __init__(self):
        self.messages, self.calls = self, 0

    def create(self, model, system, messages, **kw):
        self.calls += 1
        doc = messages[0]["content"]
        if "earnings" in doc:
            label, quote = testset.LABELS["2.02"], "The company reported earnings."
        else:
            label, quote = testset.LABELS["8.01"], "made up sentence"
        if self.calls == 3:
            label = compare.NEI
        return NS(content=[NS(type="text", text=json.dumps({"label": label, "quote": quote}))],
                  stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=200))


def write(tmp_path):
    rows = [{"id": "8k-a", "filed": "2015-01-01", "label": "2.02", "text": "The company reported   earnings.\nMore."},
            {"id": "8k-b", "filed": "2016-01-01", "label": "1.01", "text": "An agreement was signed."}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_correct_needs_label_and_verified_quote_and_records_experiments(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "CANDIDATES", ["claude-haiku-5-5"])
    compare.main(write(tmp_path), tmp_path / "out", client=Fake())
    t = list(csv_rows(tmp_path / "out" / "review" / "m4_models.csv"))
    assert len(t) == 1 and float(t[0]["correct"]) == 0.5 and float(t[0]["consistent"]) == 0.5
    exp = list((tmp_path / "out" / "inbox" / "experiments").glob("*.json"))
    assert len(exp) == 1 and json.loads(exp[0].read_text())["name"] == "M4 model test: claude-haiku-5-5"


def test_budget_stops_loudly(tmp_path):
    items = [{"id": "x", "text": "t", "label": "2.02", "date": "2015-01-01", "source": "s", "tier": "filing"}]
    with pytest.raises(SystemExit, match="budget"):
        compare.run(Fake(), items, models=["claude-opus-5-5"], budget=0.01)


def csv_rows(path):
    import csv
    with open(path, newline="") as f:
        yield from csv.DictReader(f)


def test_the_pinned_sdk_takes_the_arguments_compare_sends():
    anthropic = pytest.importorskip("anthropic")  # installed on the runners from requirements.txt
    import inspect
    params = inspect.signature(anthropic.resources.messages.Messages.create).parameters
    assert {"model", "max_tokens", "system", "messages", "output_config"} <= set(params)
