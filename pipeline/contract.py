"""The data contract: every site file is checked against its JSON Schema in contract/ before upload.

A malformed file raises, so the run fails loudly instead of a bad file reaching the screen. Names are site paths
without .json; every holding/<cik>.json uses the "holding" schema.
"""
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACT = Path(__file__).resolve().parent.parent / "contract"
SCHEMAS = {"today": "today", "picks": "picks", "holdings": "holdings", "holding": "holding",
           "reports/weekly": "weekly", "reports/monthly": "monthly", "settings": "settings", "health": "health"}
_registry = Registry().with_resources(
    (p.name, Resource.from_contents(json.loads(p.read_text(encoding="utf-8")))) for p in CONTRACT.glob("*.schema.json"))
_validators = {}


class ContractError(ValueError):
    pass


def validate(name, obj):
    """Raise ContractError listing every problem if obj does not match the schema for site file `name`."""
    if name not in SCHEMAS:
        raise ContractError(f"no schema for site file {name!r}")
    if name not in _validators:
        schema = _registry.contents(f"{SCHEMAS[name]}.schema.json")
        Draft202012Validator.check_schema(schema)
        _validators[name] = Draft202012Validator(schema, registry=_registry)
    errors = sorted(_validators[name].iter_errors(obj), key=lambda e: list(e.absolute_path))
    if errors:
        raise ContractError(f"{name}.json: " + "; ".join(
            f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors[:20]))


def validate_dir(site_dir):
    """Check every file the site reads. Raises on a missing, unknown, or malformed file, a holding file named
    for the wrong CIK, or a holding in holdings.json without its detail file. Returns the number of files checked."""
    site_dir = Path(site_dir)
    found = {p.relative_to(site_dir).as_posix()[:-5]: p for p in site_dir.rglob("*.json")}
    missing = [n for n in SCHEMAS if n != "holding" and n not in found]
    if missing:
        raise ContractError(f"missing site files: {missing}")
    held = set()
    for rel, p in sorted(found.items()):
        name = "holding" if rel.startswith("holding/") else rel
        obj = json.loads(p.read_text(encoding="utf-8"))
        validate(name, obj)
        if name == "holding":
            if rel != f"holding/{obj['cik']}":
                raise ContractError(f"{rel}.json holds cik {obj['cik']}")
            held.add(obj["cik"])
    unlinked = {h["cik"] for h in json.loads(found["holdings"].read_text(encoding="utf-8"))["holdings"]} - held
    if unlinked:
        raise ContractError(f"holdings without holding/<cik>.json: {sorted(unlinked)}")
    return len(found)
