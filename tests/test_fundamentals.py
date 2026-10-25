import json
import zipfile

import pandas as pd

from pipeline.fundamentals import extract


def facts(cik, assets_obs, other_obs):
    return {"cik": cik, "entityName": "X", "facts": {"us-gaap": {
        "Assets": {"units": {"USD": assets_obs}},
        "SomethingObscure": {"units": {"USD": other_obs}},
    }}}


def obs(end, val, form, filed, accn):
    return {"end": end, "val": val, "accn": accn, "fy": 2015, "fp": "FY", "form": form, "filed": filed}


def test_extract_keeps_every_filed_copy_of_whitelisted_tags_for_companies_in_scope(tmp_path):
    zp, dest = tmp_path / "f.zip", tmp_path / "facts.parquet"
    with zipfile.ZipFile(zp, "w") as z:
        # the same 2015 number, first reported in 2016 and repeated (restated) in the 2017 10-K
        z.writestr("CIK0000000001.json", json.dumps(facts(1, [obs("2015-12-31", 100, "10-K", "2016-02-01", "a1"),
                                                              obs("2015-12-31", 98, "10-K", "2017-02-01", "a2")],
                                                          [obs("2015-12-31", 5, "10-K", "2016-02-01", "a1")])))
        z.writestr("CIK0000000002.json", json.dumps(facts(2, [obs("2015-12-31", 7, "10-K", "2016-02-01", "b1")], [])))
    n_co, n_rows, cover, no_facts = extract(zp, scope={1, 3}, tuning_ciks={1, 3}, dest=dest)
    f = pd.read_parquet(dest)
    assert (n_co, n_rows) == (1, 2)  # company 2 is out of scope; SomethingObscure isn't whitelisted
    assert list(zip(f["filed"], f["value"])) == [("2016-02-01", 100.0), ("2017-02-01", 98.0)]
    assert cover[("us-gaap", "Assets")] == 1 and cover[("us-gaap", "SomethingObscure")] == 1
    assert no_facts == {3}
