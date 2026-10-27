"""Code checks on the panel's claims (ARCHITECTURE "Agent flow" steps 4 and 5).

A quote must appear in its document word for word: only whitespace and curly-vs-straight quote marks are
normalized. Every number in a claim must equal a number from our database or one in its own quote (sign and
thousands commas ignored, no rounding). Failing claims are removed and returned with the reason, for counting.
"""
import re

QUOTES = str.maketrans({c: "'" for c in "‘’‚‛"} | {c: '"' for c in "“”„‟"})
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def normalize(s):
    return re.sub(r"\s+", " ", s.translate(QUOTES)).strip()


def numbers(s):
    return {float(n.replace(",", "")) for n in NUMBER.findall(s)}


def verify(claims, docs, db_numbers=()):
    """(kept, removed). docs: {doc_id: text}. removed: [(reason, claim)], reason 'quote' or 'number'."""
    db = {abs(float(n)) for n in db_numbers}
    texts = {k: normalize(v) for k, v in docs.items()}
    kept, removed = [], []
    for c in claims:
        q = normalize(c["quote"])
        if not q or q not in texts.get(c["doc_id"], ""):
            removed.append(("quote", c))
        elif not numbers(c["claim"]) <= db | numbers(c["quote"]):
            removed.append(("number", c))
        else:
            kept.append(c)
    return kept, removed


def rerun_check(first, second):
    """The first answer, marked low confidence if a rerun on identical input gave a different verdict."""
    return {**first, "low_confidence": first["verdict"] != second["verdict"]}
