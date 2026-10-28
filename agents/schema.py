"""The panel's fixed output schema (ARCHITECTURE "Agent flow" step 3) and a strict parser.

An answer is one JSON object, nothing around it: {"verdict", "confidence", "claims": [{"claim", "quote", "doc_id"}]}.
Verdicts use the math's words (PLAN "Verdicts") so the two can be compared, plus "Not enough information", which
needs no claims. Anything else is rejected, never repaired.
"""
import json

NOT_OWNED = ("Buy", "Wait", "Don't buy")
OWNED = ("Hold", "Add", "Trim", "Sell")
NEI = "Not enough information"
KEYS = {"verdict", "confidence", "claims"}
CLAIM_KEYS = {"claim", "quote", "doc_id"}
SIDES = ("for", "against")


class SchemaError(ValueError):
    pass


def _const(c):
    raise SchemaError(f"{c} is not allowed")


def parse(raw, doc_ids, owned=None, sides=False):
    """The answer as a dict, or SchemaError. doc_ids: the documents the agent was given. owned: True or False limits
    the verdict to that side's words; None allows either. sides: each claim also needs "side", "for" or "against"
    (the judge's claims, as the site shows them)."""
    try:
        a = json.loads(raw, parse_constant=_const)
    except json.JSONDecodeError as e:
        raise SchemaError(f"not one JSON object: {e}") from e
    if not isinstance(a, dict) or set(a) != KEYS:
        raise SchemaError(f"answer must have exactly the keys {sorted(KEYS)}")
    allowed = {True: OWNED, False: NOT_OWNED, None: OWNED + NOT_OWNED}[owned] + (NEI,)
    if a["verdict"] not in allowed:
        raise SchemaError(f"verdict {a['verdict']!r} not one of {allowed}")
    c = a["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
        raise SchemaError(f"confidence {c!r} must be a number from 0 to 1")
    if not isinstance(a["claims"], list) or (not a["claims"] and a["verdict"] != NEI):
        raise SchemaError("claims must be a list, empty only for 'Not enough information'")
    keys = CLAIM_KEYS | ({"side"} if sides else set())
    for cl in a["claims"]:
        if not isinstance(cl, dict) or set(cl) != keys:
            raise SchemaError(f"each claim must have exactly the keys {sorted(keys)}")
        if sides and cl["side"] not in SIDES:
            raise SchemaError(f"side {cl['side']!r} not one of {SIDES}")
        if not all(isinstance(cl[k], str) and cl[k].strip() for k in CLAIM_KEYS):
            raise SchemaError("claim, quote and doc_id must be non-empty strings")
        if cl["doc_id"] not in doc_ids:
            raise SchemaError(f"claim cites unknown document {cl['doc_id']!r}")
    return a
