from datetime import date, datetime, timedelta, timezone

from monitor import failures

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def run(day, conclusion="success", event="schedule", name="nightly", hour="01:17"):
    return {"workflowName": name, "event": event, "status": "completed", "conclusion": conclusion,
            "createdAt": f"{day}T{hour}:00Z", "url": "u"}


def nightly_every_day(start=date(2026, 10, 9), n=33):
    return [run((start + timedelta(days=i)).isoformat()) for i in range(n)]


def test_a_clean_month_has_no_failures():
    assert failures.find(nightly_every_day(), NOW) == []
    assert failures.summary([]) == "None in the last 30 days."


def test_a_missing_night_counts_but_a_late_start_doesnt():
    runs = [r for r in nightly_every_day() if r["createdAt"][:10] != "2026-11-04"]
    found = failures.find(runs, NOW)
    assert [(f["date"], f["job"]) for f in found] == [("2026-11-03", "nightly")]  # Tuesday's night is gone
    runs.append(run("2026-11-04", hour="09:40"))  # GitHub started it hours late: still that night
    assert failures.find(runs, NOW) == []


def test_weekend_nights_arent_trading_days():
    runs = [r for r in nightly_every_day() if r["createdAt"][:10] not in ("2026-11-08", "2026-11-09")]
    assert failures.find(runs, NOW) == []  # Saturday and Sunday nights (the Friday run is on Saturday 01:17)


def test_failed_scheduled_runs_count_even_if_rescued_by_hand():
    runs = nightly_every_day() + [run("2026-11-05", "failure", hour="07:02"), run("2026-11-05", event="workflow_dispatch",
                                                                               hour="08:00")]
    assert [f["what"] for f in failures.find(runs, NOW)] == ["scheduled run ended in failure"]


def test_cancelled_manual_and_old_runs_dont_count():
    runs = nightly_every_day() + [run("2026-11-06", "cancelled", name="watch"),
                                  run("2026-11-06", "failure", event="workflow_dispatch", name="weekly"),
                                  run("2026-09-01", "failure", name="watch")]
    assert failures.find(runs, NOW) == []


def test_nothing_before_the_paper_period_counts():
    early = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    assert failures.find([run("2026-10-29", "failure", hour="07:02"), run("2026-11-02")], early) == []


def test_llm_over_cap():
    found = failures.find(nightly_every_day(), NOW, llm_usd=10.5, cap=10)
    assert found[0]["job"] == "llm"


def test_runs_the_cloudflare_scheduler_started_count_as_scheduled():
    cron = {**run("2026-11-05", "failure", event="workflow_dispatch"), "displayTitle": "nightly (scheduled)"}
    by_hand = {**run("2026-11-06", "failure", event="workflow_dispatch"), "displayTitle": "nightly"}
    found = failures.find(nightly_every_day() + [cron, by_hand], NOW)
    assert [f["date"] for f in found] == ["2026-11-05"]
