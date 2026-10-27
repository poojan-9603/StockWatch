"""M4 model choice: each candidate reads the 98 test 8-Ks and names the event, quoting the sentence that shows it.

One call per document per run, no tools, structured output. The answer counts only if the label is right AND code
finds the quote word for word in the document (agents.verify). Each document runs twice to measure consistency.
The choice rule is fixed in docs/M4.md before any results.

Usage: python -m agents.compare TESTSET.jsonl OUT_DIR
  writes OUT_DIR/review/m4_models.csv, m4_answers.jsonl and inbox/experiments/*.json
"""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from agents import docs, testset, verify
from agents.schema import NEI

CANDIDATES = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
RUNS = 2
EFFORT = "low"  # a classification; the same for every model so the comparison is fair
BUDGET = 6.00  # dollars for the whole comparison (estimate about $3 to $5); stops before a call that could pass it
LABELS = sorted(testset.LABELS.values()) + [NEI]

SYSTEM = ("You read one SEC Form 8-K filing and say which event it reports. Choose exactly one label from the list. "
          "Copy one sentence from the document, word for word, that shows the event. If the document doesn't say "
          f"what happened, answer {NEI!r} with an empty quote.\n\nLabels:\n" + "\n".join(f"- {x}" for x in LABELS))
FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False, "required": ["label", "quote"],
    "properties": {"label": {"type": "string", "enum": LABELS}, "quote": {"type": "string"}}}}


def ask(client, model, doc):
    """(answer dict, input tokens, output tokens). Refusals and bad output count as wrong, never retried around."""
    r = client.messages.create(model=model, max_tokens=4000, system=SYSTEM, output_config={"effort": EFFORT,
                               "format": FORMAT},
                               messages=[{"role": "user", "content": docs.assemble([doc])}])
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        a = json.loads(text) if r.stop_reason == "end_turn" else None
    except json.JSONDecodeError:
        a = None
    a = a if isinstance(a, dict) else {"label": None, "quote": "", "error": r.stop_reason}
    return a, r.usage.input_tokens, r.usage.output_tokens


def score(a, doc):
    """One answer scored: label right, quote found in the document, both."""
    right = a.get("label") == testset.LABELS[doc["label"]]
    kept, _ = verify.verify([{"claim": "", "quote": a.get("quote") or "", "doc_id": doc["id"]}],
                            {doc["id"]: doc["text"]})
    return {"right": right, "quoted": bool(kept), "correct": right and bool(kept), "nei": a.get("label") == NEI}


def run(client, items, models=None, runs=RUNS, workers=6, budget=BUDGET):
    """Every model on every item, `runs` times. Stops loudly once a call could take spending past `budget`."""
    spent, answers, lock = [0.0], [], threading.Lock()

    def one(model, d, k):
        pin, pout = PRICE[model]
        with lock:
            if spent[0] + (8000 * pin + 4000 * pout) / 1e6 > budget:  # worst case for one call
                return None
        a, tin, tout = ask(client, model, d)
        cost = (tin * pin + tout * pout) / 1e6
        with lock:
            spent[0] += cost
        return {"model": model, "run": k, "id": d["id"], "label": d["label"], "answer": a, "cost": cost} | score(a, d)

    for model in models or CANDIDATES:
        jobs = [(d, k) for k in range(runs) for d in items]
        with ThreadPoolExecutor(workers) as pool:
            done = list(pool.map(lambda j: one(model, *j), jobs))
        answers += [x for x in done if x]
        print(f"{model}: {sum(1 for x in done if x)} of {len(jobs)} calls, ${spent[0]:.2f} spent so far")
        if not all(done):
            raise SystemExit(f"budget of ${budget} reached during {model}; results so far are not a fair comparison")
    return pd.DataFrame(answers)


def table(ans):
    rows = []
    for model, g in ans.groupby("model", sort=False):
        first = g[g["run"] == 0]
        labels = g.assign(l=g["answer"].map(lambda a: a.get("label"))).pivot(index="id", columns="run", values="l")
        rows.append({"model": model, "correct": first["correct"].mean(), "right_label": first["right"].mean(),
                     "quote_found": first["quoted"].mean(), "not_enough_info": first["nei"].mean(),
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
        params = {"model": r["model"], "runs": RUNS, "effort": EFFORT, "documents": len(items)}
        (exp / f"{now[:10]}-m4-{r['model']}.json").write_text(json.dumps(
            {"created_at": now, "name": f"M4 model test: {r['model']}", "params": json.dumps(params, sort_keys=True),
             "result": json.dumps(r, sort_keys=True), "note": "8-K event test set, headings masked"}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
