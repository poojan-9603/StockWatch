"""M4 model choice: each candidate reads the 98 test 8-Ks and names the event, quoting the sentence that shows it.

One request per document per run, no tools, structured output, sent through the Message Batches API (half price;
most batches finish within an hour). The answer counts only if the label is right AND code finds the quote word for
word in the document (agents.verify). Each document runs twice to measure consistency. The choice rule is fixed in
docs/M4.md before any results.

Usage: python -m agents.compare TESTSET.jsonl OUT_DIR
  writes OUT_DIR/review/m4_models.csv, m4_answers.jsonl and inbox/experiments/*.json
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from agents import docs, testset, verify
from agents.schema import NEI

CANDIDATES = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
BATCH_DISCOUNT = 0.5
RUNS = 2
EFFORT = "low"  # a classification; the same for every model so the comparison is fair
MAX_TOKENS = 1500  # thinking counts toward it; an answer cut off here counts as wrong and is reported
BUDGET = 6.00  # dollars, checked against the worst case before anything is sent (expected about $1.50 to $2.50)
WAIT_HOURS = 5  # the workflow's limit is 6; batches can take up to 24
LABELS = sorted(testset.LABELS.values()) + [NEI]

SYSTEM = ("You read one SEC Form 8-K filing and say which event it reports. Choose exactly one label from the list. "
          "Copy one sentence from the document, word for word, that shows the event. If the document doesn't say "
          f"what happened, answer {NEI!r} with an empty quote.\n\nLabels:\n" + "\n".join(f"- {x}" for x in LABELS))
FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False, "required": ["label", "quote"],
    "properties": {"label": {"type": "string", "enum": LABELS}, "quote": {"type": "string"}}}}


def request(model, doc, k):
    return {"custom_id": f"{doc['id']}_r{k}", "params": {
        "model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
        "output_config": {"effort": EFFORT, "format": FORMAT},
        "messages": [{"role": "user", "content": docs.assemble([doc])}]}}


def worst_cost(model, items, runs):
    """Upper bound in dollars: 3 characters a token (English runs about 4) and every answer at MAX_TOKENS."""
    pin, pout = PRICE[model]
    tokens_in = sum(len(SYSTEM) + len(docs.assemble([d])) for d in items) / 3
    return runs * (tokens_in * pin + len(items) * MAX_TOKENS * pout) / 1e6 * BATCH_DISCOUNT


def answer(result):
    """(answer dict, input tokens, output tokens) from one batch result. Refusals, cut-off answers, errors and bad
    output count as wrong, never retried around."""
    if result.result.type != "succeeded":
        return {"label": None, "quote": "", "error": result.result.type}, 0, 0
    m = result.result.message
    text = next((b.text for b in m.content if b.type == "text"), "")
    try:
        a = json.loads(text) if m.stop_reason == "end_turn" else None
    except json.JSONDecodeError:
        a = None
    a = a if isinstance(a, dict) else {"label": None, "quote": "", "error": m.stop_reason}
    return a, m.usage.input_tokens, m.usage.output_tokens


def score(a, doc):
    """One answer scored: label right, quote found in the document, both."""
    right = a.get("label") == testset.LABELS[doc["label"]]
    kept, _ = verify.verify([{"claim": "", "quote": a.get("quote") or "", "doc_id": doc["id"]}],
                            {doc["id"]: doc["text"]})
    return {"right": right, "quoted": bool(kept), "correct": right and bool(kept), "nei": a.get("label") == NEI}


def run(client, items, models=None, runs=RUNS, budget=BUDGET, poll=60):
    """Every model on every item, `runs` times, one batch per model. Refuses to send anything if the worst case
    could pass `budget`; stops loudly if a batch doesn't finish within WAIT_HOURS."""
    models = models or CANDIDATES
    worst = sum(worst_cost(m, items, runs) for m in models)
    if worst > budget:
        raise SystemExit(f"worst case ${worst:.2f} is over the ${budget} budget; nothing sent")
    print(f"worst case ${worst:.2f}, budget ${budget}")
    batches = {m: client.messages.batches.create(requests=[request(m, d, k) for k in range(runs) for d in items]).id
               for m in models}
    deadline = time.time() + WAIT_HOURS * 3600
    while any(client.messages.batches.retrieve(b).processing_status != "ended" for b in batches.values()):
        if time.time() > deadline:
            for b in batches.values():
                client.messages.batches.cancel(b)
            raise SystemExit(f"batches not finished after {WAIT_HOURS} hours; canceled: {list(batches.values())}")
        time.sleep(poll)
    by_id, answers = {d["id"]: d for d in items}, []
    for model, b in batches.items():
        pin, pout = PRICE[model]
        for r in client.messages.batches.results(b):
            doc_id, k = r.custom_id.rsplit("_r", 1)
            d = by_id[doc_id]
            a, tin, tout = answer(r)
            cost = (tin * pin + tout * pout) / 1e6 * BATCH_DISCOUNT
            answers.append({"model": model, "run": int(k), "id": doc_id, "label": d["label"], "answer": a,
                            "cost": cost} | score(a, d))
        got = [x for x in answers if x["model"] == model]
        print(f"{model}: {len(got)} answers, {sum('error' in x['answer'] for x in got)} failed, "
              f"${sum(x['cost'] for x in got):.2f}")
    return pd.DataFrame(answers)


def table(ans):
    rows = []
    for model, g in ans.groupby("model", sort=False):
        first = g[g["run"] == 0]
        label = g["answer"].map(lambda a: a.get("label") or "failed")  # a failed answer never counts as consistent
        labels = g.assign(l=label).pivot(index="id", columns="run", values="l")
        rows.append({"model": model, "correct": first["correct"].mean(), "right_label": first["right"].mean(),
                     "quote_found": first["quoted"].mean(), "not_enough_info": first["nei"].mean(),
                     "failed": first["answer"].map(lambda a: "error" in a).mean(),
                     "consistent": (labels.nunique(axis=1) == 1).mean(), "cost_per_doc": g["cost"].sum() / len(g),
                     "worst_label": first.groupby("label")["correct"].mean().idxmin(),
                     "worst_label_correct": first.groupby("label")["correct"].mean().min()})
    return pd.DataFrame(rows).round(4)


def main(testset_path, out_dir, client=None):
    if client is None:
        import anthropic  # only here, so tests and the pipeline don't need the SDK
        client = anthropic.Anthropic(max_retries=6)
    items = []
    for line in Path(testset_path).read_text(encoding="utf-8").splitlines():
        t = json.loads(line)
        d = docs.document(t["text"], t["id"], t["filed"], "SEC EDGAR", "filing", html=False)
        items.append(d | {"label": t["label"]})
    ans = run(client, items)
    out = Path(out_dir)
    (out / "review").mkdir(parents=True, exist_ok=True)
    ans.to_json(out / "review" / "m4_answers.jsonl", orient="records", lines=True)
    t = table(ans)
    t.to_csv(out / "review" / "m4_models.csv", index=False)
    print(t.to_string(index=False))
    exp = out / "inbox" / "experiments"
    exp.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in t.to_dict("records"):
        params = {"model": r["model"], "runs": RUNS, "effort": EFFORT, "max_tokens": MAX_TOKENS, "batch": True,
                  "documents": len(items)}
        (exp / f"{now[:19].replace(':', '')}-m4-{r['model']}.json").write_text(json.dumps(
            {"created_at": now, "name": f"M4 model test: {r['model']}", "params": json.dumps(params, sort_keys=True),
             "result": json.dumps(r, sort_keys=True), "note": "8-K event test set, headings masked"}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
