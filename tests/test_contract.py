import copy
import json
import shutil

import pytest

from pipeline.contract import CONTRACT, SCHEMAS, ContractError, validate, validate_dir

EXAMPLES = CONTRACT / "examples"


def load(rel):
    return json.loads((EXAMPLES / f"{rel}.json").read_text(encoding="utf-8"))


def test_every_schema_has_an_example_and_all_validate():
    assert validate_dir(EXAMPLES) == 9
    assert {"holding" if p.parent.name == "holding" else p.relative_to(EXAMPLES).as_posix()[:-5]
            for p in EXAMPLES.rglob("*.json")} == set(SCHEMAS)


def broken(rel, change):
    obj = copy.deepcopy(load(rel))
    change(obj)
    return obj


@pytest.mark.parametrize("name, rel, change, says", [
    ("today", "today", lambda o: o.pop("generated_at"), "generated_at"),
    ("today", "today", lambda o: o.update(extra=1), "Additional properties"),
    ("today", "today", lambda o: o.update(quiet=o["quiet"] * 2), "too long"),
    ("today", "today", lambda o: o.update(status="blocked"), "blocked_reason"),
    ("picks", "picks", lambda o: o["picks"][0]["math"].update(score=150), "150"),
    ("picks", "picks", lambda o: o["picks"][0].update(verdict="Hold"), "Hold"),
    ("picks", "picks", lambda o: o["picks"][0].update(size_pct=None), "size_pct"),
    ("picks", "picks", lambda o: o["picks"][0]["math"]["ranks"].pop("issuance"), "issuance"),
    ("holdings", "holdings", lambda o: o["holdings"][0].update(verdict="Buy"), "Buy"),
    ("holding", "holding/1", lambda o: o["lots"][0].update(long_term="no"), "long_term"),
    ("holding", "holding/1", lambda o: o["panel"]["claims"][0].pop("quote"), "quote"),
    ("settings", "settings", lambda o: o.update(as_of="26 Oct 2026"), "as_of"),
    ("reports/monthly", "reports/monthly", lambda o: o.update(mode="live"), "live"),
])
def test_malformed_files_are_rejected(name, rel, change, says):
    with pytest.raises(ContractError, match=says):
        validate(name, broken(rel, change))


def test_dir_rejects_unknown_missing_and_misnamed_files(tmp_path):
    site = tmp_path / "site"
    shutil.copytree(EXAMPLES, site)
    (site / "stray.json").write_text("{}")
    with pytest.raises(ContractError, match="no schema"):
        validate_dir(site)
    (site / "stray.json").unlink()
    (site / "holding" / "1.json").rename(site / "holding" / "7.json")
    with pytest.raises(ContractError, match="cik 1"):
        validate_dir(site)
    (site / "holding" / "7.json").unlink()
    with pytest.raises(ContractError, match=r"without holding/<cik>.json: \[1\]"):
        validate_dir(site)
    (site / "health.json").unlink()
    with pytest.raises(ContractError, match="missing site files"):
        validate_dir(site)
