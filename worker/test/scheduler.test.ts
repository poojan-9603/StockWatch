import assert from "node:assert/strict";
import { test } from "node:test";
import scheduler, { dispatch, due } from "../src/scheduler.ts";

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

test("a job GitHub accepted but never ran is started again, one and two hours later, never the watch", async () => {
  const real = globalThis.fetch;
  const env = { GITHUB_TOKEN: "t", REPO: "o/r" };
  let runs: object[] = [];
  let jobs = 0;
  const seen: string[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    seen.push(`${init?.method ?? "GET"} ${url}`);
    if (url.endsWith("/dispatches")) return new Response(null, { status: 204 });
    if (url.includes("/jobs")) return Response.json({ total_count: jobs });
    return Response.json({ workflow_runs: runs });
  }) as typeof fetch;
  const tick = async (iso: string) => {
    seen.length = 0;
    await scheduler.scheduled({ scheduledTime: Date.parse(iso) }, env);
    return seen.filter((s) => s.startsWith("POST")).map((s) => s.split("/workflows/")[1]);
  };
  try {
    // Tuesday 04:17: the 03:17 nightly failed with no job, as the Nov 9 watch did
    runs = [{ id: 9, status: "completed", conclusion: "failure" }];
    assert.deepEqual(await tick("2026-11-10T04:17:00Z"), ["nightly.yml/dispatches"]);
    assert.ok(seen.some((s) => s.includes("nightly.yml/runs?per_page=5&created=%3E%3D2026-11-10T03%3A17%3A00.000Z")));
    jobs = 1; // it ran a job and failed: a real failure, left for Health, not retried
    assert.deepEqual(await tick("2026-11-10T04:17:00Z"), []);
    runs = []; // the start never became a run
    assert.deepEqual(await tick("2026-11-10T05:17:00Z"), ["nightly.yml/dispatches"]);
    runs = [{ id: 10, status: "in_progress", conclusion: null }];
    assert.deepEqual(await tick("2026-11-10T05:17:00Z"), []);
    runs = [];
    assert.deepEqual(await tick("2026-11-10T06:17:00Z"), []); // three hours on: no more tries
    assert.deepEqual(await tick("2026-11-10T14:17:00Z"), ["watch.yml/dispatches"]); // the watch is never checked
    assert.ok(!seen.some((s) => s.includes("watch.yml/runs")));
  } finally {
    globalThis.fetch = real;
  }
});
