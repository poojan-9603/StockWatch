import assert from "node:assert/strict";
import { test } from "node:test";
import { dispatch, due } from "../src/scheduler.ts";

const at = (iso: string) => due(new Date(iso));

test("due matches the old GitHub crons, hour by hour, for a whole week", () => {
  const want: Record<string, number> = { nightly: 5, watch: 65, weekly: 1, restore: 1, prices: 1, backfill: 1 };
  const got: Record<string, number> = {};
  for (let h = 0; h < 24 * 7; h++)
    for (const j of due(new Date(Date.UTC(2026, 10, 1, h, 17)))) got[j] = (got[j] ?? 0) + 1; // Sun Nov 1 to Sat Nov 7
  assert.deepEqual(got, want);
  assert.deepEqual(at("2026-11-03T03:17:00Z"), ["nightly"]); // Tuesday 03:17, Monday's nightly
  assert.deepEqual(at("2026-11-03T01:17:00Z"), []); // not 01:17 any more
  assert.deepEqual(at("2026-11-02T03:17:00Z"), []); // Monday 03:17: no Sunday nightly
  assert.deepEqual(at("2026-11-07T00:17:00Z"), ["watch"]); // Saturday 00:17 is Friday 7 pm Central
  assert.deepEqual(at("2026-11-07T12:17:00Z"), ["restore"]); // Saturday: restore, no watch
  assert.deepEqual(at("2026-11-01T15:17:00Z"), ["weekly"]);
  assert.deepEqual(at("2026-11-07T09:17:00Z"), ["prices"]);
  assert.deepEqual(at("2026-11-07T06:17:00Z"), ["backfill"]);
});

test("dispatch sends by=cron, retries, then fails loudly", async () => {
  const real = globalThis.fetch;
  const calls: { url: string; body: string; auth: string }[] = [];
  let answers = [500, 204];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, body: String(init.body), auth: (init.headers as Record<string, string>).authorization });
    return new Response(null, { status: answers.shift() ?? 403 });
  }) as typeof fetch;
  try {
    const env = { GITHUB_TOKEN: "t", REPO: "o/r" };
    await dispatch(env, "nightly", 0);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].url, "https://api.github.com/repos/o/r/actions/workflows/nightly.yml/dispatches");
    assert.deepEqual(JSON.parse(calls[1].body), { ref: "main", inputs: { by: "cron" } });
    assert.equal(calls[1].auth, "Bearer t");
    answers = [];
    await assert.rejects(dispatch(env, "watch", 0), /watch: GitHub refused the start 3 times: HTTP 403/);
    await assert.rejects(dispatch({ REPO: "o/r" }, "watch", 0), /GITHUB_TOKEN is not set/);
  } finally {
    globalThis.fetch = real;
  }
});
