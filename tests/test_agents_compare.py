"""M4 model comparison with a fake batch client: scoring needs the right label AND a verified quote, and the budget
check refuses before anything is sent."""
import csv
import json
from types import SimpleNamespace as NS

import pytest

from agents import compare, testset


class FakeBatches:
    """Answers 2.02 right with a real quote, everything else with a made-up quote; run 1 of 8k-a says NEI, and run 1
    of 8k-b is cut off at max_tokens."""
    def __init__(self):
        self.made, self.polls = {}, 0

    def create(self, requests):
        bid = f"b{len(self.made)}"
        self.made[bid] = requests
        return NS(id=bid)

    def retrieve(self, bid):
        self.polls += 1
        return NS(processing_status="ended" if self.polls > 1 else "in_progress")

    def cancel(self, bid):
        raise AssertionError("nothing should be canceled")

    def results(self, bid):
        for r in self.made[bid]:
            doc = r["params"]["messages"][0]["content"]
            label, quote, stop = testset.LABELS["8.01"], "made up sentence", "end_turn"
            if "earnings" in doc:
                label, quote = testset.LABELS["2.02"], "The company reported earnings."
            if r["custom_id"] == "8k-a_r1":
                label = compare.NEI
            if r["custom_id"] == "8k-b_r1":
                stop = "max_tokens"
            msg = NS(content=[NS(type="text", text=json.dumps({"label": label, "quote": quote}))], stop_reason=stop,
                     usage=NS(input_tokens=1000, output_tokens=200))
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


class Fake:
    def __init__(self):
        self.messages = NS(batches=FakeBatches())


def write(tmp_path):
    rows = [{"id": "8k-a", "filed": "2015-01-01", "label": "2.02", "text": "The company reported   earnings.\nMore."},
            {"id": "8k-b", "filed": "2016-01-01", "label": "1.01", "text": "An agreement was signed."}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_correct_needs_label_and_verified_quote_and_records_experiments(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "CANDIDATES", ["claude-haiku-5-5"])
    monkeypatch.setattr(compare.time, "sleep", lambda s: None)
    client = Fake()
    compare.main(write(tmp_path), tmp_path / "out", client=client)
    sent = client.messages.batches.made["b0"]
    assert len(sent) == 4 and {r["params"]["model"] for r in sent} == {"claude-haiku-5-5"}
    with open(tmp_path / "out" / "review" / "m4_models.csv", newline="") as f:
        t = list(csv.DictReader(f))
    assert len(t) == 1 and float(t[0]["correct"]) == 0.5 and float(t[0]["consistent"]) == 0.0
    assert float(t[0]["failed"]) == 0.0  # the cut-off answer is in run 1; the table's rates use run 0
    ans = [json.loads(x) for x in (tmp_path / "out" / "review" / "m4_answers.jsonl").read_text().splitlines()]
    assert next(a for a in ans if a["id"] == "8k-b" and a["run"] == 1)["answer"]["error"] == "max_tokens"
    exp = list((tmp_path / "out" / "inbox" / "experiments").glob("*.json"))
    assert len(exp) == 1 and json.loads(exp[0].read_text())["name"] == "M4 model test: claude-haiku-5-5"


def test_budget_refuses_before_sending_anything():
    items = [{"id": "x", "text": "t" * 30000, "label": "2.02", "date": "2015-01-01", "source": "s", "tier": "filing"}]
    client = Fake()
    with pytest.raises(SystemExit, match="nothing sent"):
        compare.run(client, items, models=["claude-opus-5-5"], budget=0.01)
    assert client.messages.batches.made == {}


def test_the_pinned_sdk_takes_the_arguments_compare_sends():
    anthropic = pytest.importorskip("anthropic")  # installed on the runners from requirements.txt
    import inspect
    params = inspect.signature(anthropic.resources.messages.Messages.create).parameters
    assert {"model", "max_tokens", "system", "messages", "output_config"} <= set(params)
    assert "requests" in inspect.signature(anthropic.resources.messages.Batches.create).parameters
