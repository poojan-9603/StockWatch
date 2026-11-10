// Scheduler Worker: GitHub drops or delays most scheduled runs (docs/M7.md), so Cloudflare's cron starts the core jobs
// instead, at :17 every hour, through GitHub's workflow_dispatch API with by=cron. The jobs keep their own gates
// (watch hours, first weekend, trading days). It holds one secret, GITHUB_TOKEN: a fine-grained token for this one
// repository with Actions read and write, so it can start runs and nothing else (no code, no secrets).

interface Env {
  GITHUB_TOKEN?: string;
  REPO: string; // owner/name
}

// The old GitHub crons, in UTC (day 0 = Sunday): nightly "17 3 * * 2-6" (01:17 until 2026-11-05),
// watch "17 12-23 * * 1-5" and "17 0 * * 2-6", weekly "17 15 * * 0", restore "17 12 * * 6", prices "17 9 * * 6", backfill "17 6 * * 6".
export function due(t: Date): string[] {
  const h = t.getUTCHours(), d = t.getUTCDay(), weekday = d >= 1 && d <= 5, tueToSat = d >= 2;
  const jobs: string[] = [];
  if (h === 3 && tueToSat) jobs.push("nightly"); // 03:17: Yahoo leaves the close blank until about 02:20 (docs/M2.md)
  if ((h >= 12 && weekday) || (h === 0 && tueToSat)) jobs.push("watch");
  if (h === 15 && d === 0) jobs.push("weekly");
  if (h === 12 && d === 6) jobs.push("restore");
  if (h === 9 && d === 6) jobs.push("prices");
  if (h === 6 && d === 6) jobs.push("backfill");
  return jobs;
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

const HEADERS = (env: Env) => ({
  authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json",
  "user-agent": "stockwatch-scheduler", "x-github-api-version": "2022-11-28",
});

async function get(env: Env, path: string) {
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/${path}`, { headers: HEADERS(env) });
  if (!r.ok) throw new Error(`GET ${path}: HTTP ${r.status}`);
  return r.json() as Promise<any>;
}

// GitHub sometimes accepts a start and never runs it: on 2026-11-09 a watch run got no job and no log (docs/M7.md).
// One and two hours after each job was due, check that a run of it started since then, and start it again if not.
// The hourly watch is left out (the next hour runs anyway); a nightly retried at 04:17 or 05:17 still uses full closes.
export async function missed(env: Env, job: string, since: Date): Promise<boolean> {
  const { workflow_runs: runs } = await get(env,
    `actions/workflows/${job}.yml/runs?per_page=5&created=${encodeURIComponent(">=" + since.toISOString())}`);
  if (!runs.length) return true; // the start never became a run
  const last = runs[0]; // newest first: a retry that did start counts
  if (last.status !== "completed" || !["failure", "startup_failure"].includes(last.conclusion)) return false;
  return (await get(env, `actions/runs/${last.id}/jobs`)).total_count === 0; // failed without ever running a job
}

// Three tries per job, then a thrown error, which marks the cron run failed in Cloudflare's log. A night that still
// doesn't run shows up on Health as a missing night (monitor/failures.py).
export async function dispatch(env: Env, job: string, pause = 5000) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not set");
  let last = "";
  for (let i = 1; i <= 3; i++) {
    try {
      const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/${job}.yml/dispatches`, {
        method: "POST",
        headers: HEADERS(env),
        body: JSON.stringify({ ref: "main", inputs: { by: "cron" } }),
      });
      if (r.ok) return;
      last = `HTTP ${r.status} ${(await r.text()).slice(0, 200)}`;
    } catch (e) {
      last = String(e);
    }
    if (i < 3) await wait(pause * i);
  }
  throw new Error(`${job}: GitHub refused the start 3 times: ${last}`);
}

export default {
  async scheduled(event: { scheduledTime: number }, env: Env): Promise<void> {
    const t = event.scheduledTime;
    const failed: string[] = [];
    const start = async (job: string, why: string) => {
      try {
        await dispatch(env, job);
        console.log(`started ${job}${why}`);
      } catch (e) {
        console.error(String(e));
        failed.push(String(e));
      }
    };
    for (const job of due(new Date(t))) await start(job, "");
    for (const back of [1, 2]) {
      const then = new Date(t - back * 3600_000);
      for (const job of due(then).filter((j) => j !== "watch")) {
        try {
          if (await missed(env, job, then)) await start(job, `: GitHub never ran the ${then.toISOString()} start`);
        } catch (e) {
          console.error(`${job} check: ${e}`);
          failed.push(`${job} check: ${e}`);
        }
      }
    }
    if (failed.length) throw new Error(failed.join("; "));
  },
};
