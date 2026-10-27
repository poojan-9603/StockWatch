"""The test-years lock (CLAUDE.md hard rule 1).

Backtests, tuning and evaluation use data before TEST_START only. The one 2020+ run (M3b) happened on 2026-10-27,
unlocked by setting STOCKWATCH_UNLOCK_TEST to UNLOCK_PHRASE for that run.

Live scoring (approved by the user 2026-10-27, for paper mode and the agent panel): inside `with live(day):`, the
lock also lets through dates up to `day`, and `day` must be within LIVE_DAYS of today. So the latest day can be
scored on its full history, but no past day can be, and nothing outside the block reads 2020+ data.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

TEST_START = "2020-01-01"
UNLOCK_PHRASE = "M3b approved by the user"
LIVE_DAYS = 7  # calendar days: covers weekends and holidays before the nightly run

_live = None


def unlocked():
    return os.environ.get("STOCKWATCH_UNLOCK_TEST") == UNLOCK_PHRASE


def cutoff():
    """Exclusive upper bound on dates any quant code may read: TEST_START while locked."""
    return "9999-12-31" if unlocked() or _live else TEST_START


def check(dates):
    """Raise if any date is in the locked test years. Use on every frame a calculation consumes."""
    if not unlocked():
        late = [d for d in dates if str(d) >= TEST_START and not (_live and str(d)[:10] <= _live)]
        if late:
            raise PermissionError(f"test years are locked: {len(late)} dates from {min(late)} on")


@contextmanager
def live(day, today=None):
    """Score the latest day only. Raises if `day` is more than LIVE_DAYS before today (UTC) or after it."""
    global _live
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    earliest = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=LIVE_DAYS)).strftime("%Y-%m-%d")
    if not earliest <= str(day) <= today:
        raise PermissionError(f"live scoring is for the latest day only: {day} is not within {LIVE_DAYS} days of {today}")
    _live = str(day)
    try:
        yield
    finally:
        _live = None
