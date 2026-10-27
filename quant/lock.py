"""The test-years lock (CLAUDE.md hard rule 1).

Tuning and every measurement use data before TEST_START only. The one 2020+ run (M3b) happens after the design is
frozen and only when the user unlocks it, by setting STOCKWATCH_UNLOCK_TEST to UNLOCK_PHRASE for that run.
"""
import os

TEST_START = "2020-01-01"
UNLOCK_PHRASE = "M3b approved by the user"


def unlocked():
    return os.environ.get("STOCKWATCH_UNLOCK_TEST") == UNLOCK_PHRASE


def cutoff():
    """Exclusive upper bound on dates any quant code may read: TEST_START while locked."""
    return "9999-12-31" if unlocked() else TEST_START


def check(dates):
    """Raise if any date is in the locked test years. Use on every frame a calculation consumes."""
    if not unlocked():
        late = [d for d in dates if str(d) >= TEST_START]
        if late:
            raise PermissionError(f"test years are locked: {len(late)} dates from {min(late)} on")
