"""The agent panel with a fake batch client: roles get the right documents and models, failed quotes are dropped
and counted, a flipped rerun marks low confidence, a failed judge gives no view, and the output fits the contract."""
import json
from types import SimpleNamespace as NS

import pytest

from agents import docs, panel, schema
from pipeline import contract


class FakeBatches:
    def __init__(self, judge_fails=False):
        self.sent, self.judge_fails = [], judge_fails

    def create(self, requests):
        self.sent.append(requests)
        return NS(id=str(len(self.sent) - 1))

    def retrieve(self, bid):
        return NS(processing_status="ended")

    def cancel(self, bid):
        raise AssertionError("nothing should be canceled")

    def results(self, bid):
        for r in self.sent[int(bid)]:
            p, cid = r["params"], r["custom_id"]
            fmt = p["output_config"]["format"]["schema"]["properties"]["claims"]["items"]["properties"]
            ids = fmt["doc_id"]["enum"]
            body = p["messages"][0]["content"]
            doc_id = ids[0]
            text = body.split(f'id="{doc_id}"', 1)[1].split("\n", 2)[1]
            if cid.endswith("judge0") or cid.endswith("judge1"):
                if self.judge_fails:
                    yield NS(custom_id=cid, result=NS(type="errored"))
                    continue
                verdict = "Wait" if cid.endswith("judge1") else "Buy"
                a = {"verdict": verdict, "confidence": 0.6,
                     "claims": [{"claim": "Sales grew 12 percent", "quote": text, "doc_id": doc_id, "side": "for"},
                                {"claim": "Debt is high", "quote": "not in any document", "doc_id": doc_id,
                                 "side": "against"}]}
            else:
                a = {"verdict": "Buy", "confidence": 0.7, "claims": [{"claim": "Strong quarter", "quote": text,
                                                                       "doc_id": doc_id}]}
            msg = NS(model=p["model"], stop_reason="end_turn", content=[NS(type="text", text=json.dumps(a))],
                     usage=NS(input_tokens=2000, output_tokens=300))
            yield NS(custom_id=cid, result=NS(type="succeeded", message=msg))


def client(**kw):
    return NS(messages=NS(batches=FakeBatches(**kw)))


def stock(cik, date, tiers):
    ds = [docs.document(f"Line one of document {k}.\nSales grew 12 percent this year.", f"d{cik}-{k}", "2026-10-20",
                        f"Source {k}", t, html=False) for k, t in enumerate(tiers)]
    return {"cik": cik, "symbol": f"S{cik}", "date": date, "owned": False, "docs": ds,
            "numbers": {"revenue_growth_pct": 12}}


def date_for(cik, rerun):
    """A date on which the judge view for `cik` is (or isn't) rerun."""
    return next(d for d in (f"2026-10-{n:02d}" for n in range(1, 29)) if panel.rerun({"cik": cik, "date": d}) == rerun)


def test_roles_judge_rerun_and_failures():
    c = client()
    s0 = stock(1, date_for(1, True), ["filing", "established outlet"])
    s1 = stock(2, date_for(2, False), ["filing"])
    s2 = stock(3, "2026-10-28", [])
    views = panel.run(c, [s0, s1, s2], poll=0)
    first, second = c.messages.batches.sent
    roles = {r["custom_id"]: r["params"]["model"] for r in first}
    assert set(roles.values()) == {panel.ROUTINE}
    assert "s1-news" not in roles and "s0-news" in roles and not any(k.startswith("s2-") for k in roles)
    assert {r["custom_id"] for r in second} == {"s0-judge0", "s0-judge1", "s1-judge0"}
    assert all(r["params"]["model"] == panel.JUDGE for r in second)
    news = next(r for r in first if r["custom_id"] == "s0-news")["params"]["messages"][0]["content"]
    assert 'trust="established outlet"' in news and 'trust="filing"' not in news
    assert all("score" not in r["params"]["messages"][0]["content"].lower() for r in first + second)
    v0, v1, v2 = views
    assert v0["verdict"] == "Buy" and v0["low_confidence"] and len(v0["claims"]) == 1 and v0["claims_removed"] == 1
    assert v1["verdict"] == "Buy" and not v1["low_confidence"] and v1["opinions"]["news"]["error"] == "no documents"
    assert v2["verdict"] == schema.NEI and v2["cost"] == 0
    assert v0["cost"] > 0


def test_contract_shape_and_row():
    s = stock(2, date_for(2, False), ["filing"])
    v = panel.run(client(), [s], poll=0)[0]
    p = panel.to_contract(v, "Buy", s["docs"])
    assert p["agrees"] is True and p["claims"][0]["tier"] == "filing" and p["claims"][0]["side"] == "for"
    assert p["summary"].startswith("1 points for, 0 against") and "news no documents" in p["summary"]
    with open(contract.CONTRACT / "examples" / "holding" / "1.json", encoding="utf-8") as f:
        holding = json.load(f)
    contract.validate("holding", holding | {"panel": p})
    r = panel.row(v, "2026-10-28T00:00:00+00:00")
    assert r["verdict"] == "Buy" and json.loads(r["claims"])["claims_removed"] == 1


def test_failed_judge_gives_no_view_and_is_recorded():
    s = stock(2, "2026-10-28", ["filing"])
    v = panel.run(client(judge_fails=True), [s], poll=0)[0]
    assert v["verdict"] is None and v["error"] == "errored"
    assert panel.to_contract(v, "Buy", s["docs"]) is None
    assert json.loads(panel.row(v, "t")["claims"])["error"] == "errored"


def test_judge_claims_need_a_side():
    raw = json.dumps({"verdict": "Buy", "confidence": 0.5, "claims": [{"claim": "c", "quote": "q", "doc_id": "d"}]})
    with pytest.raises(schema.SchemaError):
        schema.parse(raw, {"d"}, owned=False, sides=True)
    ok = json.loads(raw)
    ok["claims"][0]["side"] = "for"
    assert schema.parse(json.dumps(ok), {"d"}, owned=False, sides=True)["claims"][0]["side"] == "for"


def test_removed_claims_keep_their_reason():
    s = stock(2, date_for(2, False), ["filing"])
    v = panel.run(client(), [s], poll=0)[0]
    assert v["removed_claims"] == [{"claim": "Debt is high", "quote": "not in any document", "doc_id": "d2-0",
                                    "side": "against", "reason": "quote"}]
    assert json.loads(panel.row(v, "t")["claims"])["removed_claims"][0]["reason"] == "quote"
