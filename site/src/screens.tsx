import { useState, type FormEvent } from "react";
import type {
  Action,
  Health,
  Holding,
  Holdings,
  MonthlyReport,
  Picks,
  Settings,
  Today,
  WeeklyReport,
} from "./contract";
import { Card, Load, PriceChart, Stamp, pct, usd } from "./ui";

const HOLD = ["Hold", "Add", "Trim", "Sell"];
const actionLink = (a: Action) => (HOLD.includes(a.verdict) ? `#/holding/${a.cik}` : "#/picks");

function ActionList({ actions }: { actions: Action[] }) {
  return (
    <ul className="list">
      {actions.map((a) => (
        <li key={a.cik + a.verdict}>
          <a href={actionLink(a)}>
            {a.symbol}: {a.verdict}
          </a>
          <span className="muted"> {a.summary}</span>
        </li>
      ))}
    </ul>
  );
}

export function TodayScreen() {
  return (
    <Load<Today> file="today">
      {(t) => (
        <>
          <h2>
            {t.status === "blocked"
              ? "No verdicts today."
              : t.status === "quiet"
                ? "Nothing needs you today."
                : `${t.actions.length} thing${t.actions.length === 1 ? "" : "s"} to look at.`}
          </h2>
          {t.status === "blocked" && <p className="problem">{t.blocked_reason}</p>}
          {t.alerts.map((a) => (
            <section className={`card alert ${a.tier}`} key={a.at + a.title}>
              <p className="small muted">
                {a.tier}
                {a.catalyst && ", news-driven, not score-backed"}
              </p>
              <h3>{a.cik ? <a href={`#/holding/${a.cik}`}>{a.title}</a> : a.title}</h3>
              {a.body && <p>{a.body}</p>}
            </section>
          ))}
          {t.actions.length > 0 && <ActionList actions={t.actions} />}
          {t.quiet.length > 0 && (
            <>
              <h4>Today in brief</h4>
              <ul className="list">
                {t.quiet.map((q) => (
                  <li key={q.text}>
                    {q.outcome && <span className={`tag ${q.outcome}`}>{q.outcome}</span>} {q.text}
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="small">Market: {t.market.note}</p>
          <Stamp d={t} />
        </>
      )}
    </Load>
  );
}

export function PicksScreen() {
  return (
    <Load<Picks> file="picks">
      {(p) => {
        const buys = p.picks.filter((x) => x.verdict === "Buy");
        const rest = p.picks.filter((x) => x.verdict !== "Buy");
        const fresh = buys.filter((x) => x.new_today).length;
        const large = buys.filter((x) => !x.small_cap).length;
        return (
          <>
            <h2>{buys.length ? `${buys.length} stock${buys.length === 1 ? "" : "s"} worth buying today.` : "Nothing passes the buy rule today."}</h2>
            <p className="small">
              Updated every night from that day's scores: the best-scored stocks you don't own that pass the buy rule
              {buys.length > 0 && `. ${large} in the S&P 500, ${buys.length - large} smaller`}
              {fresh > 0 && `. ${fresh} new since the last trading day`}. Stocks you own are in Holdings. Each size is
              that stock's suggested share of managed money if you buy it; nobody buys the whole list.
            </p>
            {p.decided && (
              <p className="small">
                The paper book only buys at the month-end close (last on {p.decided}), then holds until a sell signal.
              </p>
            )}
            {p.market_bad_mood && <p className="small">Market mood is poor, so the buy bar is higher and sizes are smaller.</p>}
            {[...buys, ...rest].map((x) => (
              <Card key={x.cik} title={`${x.new_today ? "New: " : ""}${x.symbol} ${x.name}`} verdict={x.verdict}
                math={x.math} panel={x.panel}>
                <p className="small">
                  {x.sector}
                  {x.size_pct !== null && `. Size ${x.size_pct}% of managed money`}
                  {x.horizon && `. Horizon ${x.horizon}`}
                  {x.review_date && `. Review ${x.review_date}`}.
                </p>
                {x.small_cap && <p className="small">Small cap: outside the backtest, so untested.</p>}
                {x.catalyst && <p className="small">News-driven, not score-backed. Your call, at most half size.</p>}
                {x.flags.map((f) => <p className="small flag" key={f}>{f}</p>)}
              </Card>
            ))}
            <h2>Big leaders down from their high</h2>
            <p className="small">
              The 100 biggest S&amp;P 500 companies, if they're in one of your themes or a strong industry and 20% or more
              below their 1-year high. A heads-up, not a buy signal: in 2013 to 2019 a dip on its own didn't beat the
              market (docs/DIPS.md), and 2020 on isn't tested.
            </p>
            {p.dips.length === 0 && <p className="small">None today.</p>}
            {p.dips.map((x) => (
              <section className="card" key={x.cik}>
                <h3>{`${x.new_today ? "New: " : ""}${x.symbol} ${x.name}, ${x.below_high_pct.toFixed(0)}% below its high`}</h3>
                <p className="small">{x.why}</p>
                <p className="small">{x.history}</p>
              </section>
            ))}
            <Stamp d={p} />
          </>
        );
      }}
    </Load>
  );
}

export function HoldingsScreen() {
  return (
    <Load<Holdings> file="holdings">
      {(h) => (
        <>
          <h2>
            {usd(h.value_usd)}, {pct(h.return_pct)} since {h.since}
          </h2>
          <p className="small">
            S&amp;P 500 {pct(h.sp500_return_pct)} over the same time. Cash {usd(h.cash_usd)}. {pct(h.drawdown_pct, false)}{" "}
            below peak.
          </p>
          {h.drawdown_alarm && <p className="problem">20% below peak: time for a review. Nothing is sold automatically.</p>}
          {h.holdings.length === 0 && <p>No holdings yet.</p>}
          {h.holdings.map((x) => (
            <Card
              key={x.cik}
              title={<a href={`#/holding/${x.cik}`}>{`${x.symbol} ${x.name}`}</a>}
              verdict={x.verdict}
              math={x.math}
              panel={x.panel}
            >
              <p className="small">
                {usd(x.value_usd)}, {x.weight_pct.toFixed(1)}% of the portfolio
                {x.target_pct !== null && ` (target ${x.target_pct}%)`}. {pct(x.return_pct)} against the S&amp;P 500's{" "}
                {pct(x.sp500_return_pct)}. Today {pct(x.day_change_pct)}.
              </p>
              {x.flags.map((f) => <p className="small flag" key={f}>{f}</p>)}
            </Card>
          ))}
          <Stamp d={h} />
        </>
      )}
    </Load>
  );
}

export function HoldingScreen({ cik }: { cik: string }) {
  return (
    <Load<Holding> file={`holding/${cik}`}>
      {(h) => (
        <>
          <p className="small"><a href="#/holdings">Holdings</a></p>
          <Card title={`${h.symbol} ${h.name}`} verdict={h.verdict} math={h.math} panel={h.panel}>
            <p className="small">
              {h.sector}. {h.shares} shares, {usd(h.value_usd)}, {h.weight_pct.toFixed(1)}% of the portfolio
              {h.target_pct !== null && `, target ${h.target_pct}%`}, cap {h.cap_pct}%.
            </p>
            <p className="small">
              {pct(h.return_pct)} against the S&amp;P 500's {pct(h.sp500_return_pct)} over the same time.
              {h.horizon && ` Horizon ${h.horizon}.`}
              {h.review_date && ` Review ${h.review_date}.`}
              {h.own_pick && " Own pick."}
            </p>
            {h.flags.map((f) => <p className="small flag" key={f}>{f}</p>)}
          </Card>
          {h.prices.length > 1 && <PriceChart prices={h.prices} />}
          <h4>Lots</h4>
          <table>
            <thead>
              <tr><th>Bought</th><th className="num">Shares</th><th className="num">Cost</th><th className="num">Return</th><th>Long-term</th></tr>
            </thead>
            <tbody>
              {h.lots.map((l) => (
                <tr key={l.lot_id}>
                  <td>{l.bought}</td>
                  <td className="num">{l.shares}</td>
                  <td className="num">{usd(l.cost_per_share)}</td>
                  <td className="num">{pct(l.return_pct)}</td>
                  <td>{l.long_term ? "yes" : `from ${l.long_term_from}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">The long-term date is a reminder, not tax advice.</p>
          <h4>Why I bought</h4>
          {h.why_bought ? (
            <>
              <p className="small">
                {h.why_bought.date}, score {h.why_bought.score ?? "not scored"}.
              </p>
              <ul className="list">{h.why_bought.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            </>
          ) : (
            <p className="small muted">No snapshot for this one.</p>
          )}
          {h.news.length > 0 && (
            <>
              <h4>News</h4>
              <ul className="list">
                {h.news.map((n) => (
                  <li key={n.date + n.title}>
                    {n.date} {n.title} <span className="muted">({n.source})</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <Stamp d={h} />
        </>
      )}
    </Load>
  );
}

export function ReportsScreen({ which }: { which: string }) {
  return (
    <>
      <nav className="tabs">
        <a href="#/reports/weekly" className={which === "weekly" ? "on" : ""}>Weekly</a>
        <a href="#/reports/monthly" className={which === "monthly" ? "on" : ""}>Monthly</a>
      </nav>
      {which === "monthly" ? <Monthly /> : <Weekly />}
    </>
  );
}

function Weekly() {
  return (
    <Load<WeeklyReport> file="reports/weekly">
      {(w) => (
        <>
          <h2>Week of {w.week_of}</h2>
          <p>{w.summary}</p>
          {w.actions.length ? <ActionList actions={w.actions} /> : <p>Nothing to do this week.</p>}
          {w.changes.length > 0 && (
            <>
              <h4>What changed</h4>
              <ul className="list">{w.changes.map((c) => <li key={c}>{c}</li>)}</ul>
            </>
          )}
          {w.disagreements.length > 0 && (
            <>
              <h4>Panel and math disagree</h4>
              <ul className="list">
                {w.disagreements.map((d) => (
                  <li key={d.cik}>
                    {d.symbol}: math {d.math_verdict}, panel {d.panel_verdict}. <span className="muted">{d.note}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <Stamp d={w} />
        </>
      )}
    </Load>
  );
}

function Monthly() {
  return (
    <Load<MonthlyReport> file="reports/monthly">
      {(m) => (
        <>
          <h2>
            {m.month}:{" "}
            {m.winning === null ? "too early to say" : m.winning ? "winning so far" : "not winning so far"}
          </h2>
          <p className="small">
            {m.mode === "paper" ? "Paper mode." : "Real money."} Since {m.since}, after costs.
          </p>
          <table>
            <thead>
              <tr><th></th><th className="num">App</th><th className="num">S&amp;P 500</th></tr>
            </thead>
            <tbody>
              <tr><td>Return</td><td className="num">{pct(m.return_pct)}</td><td className="num">{pct(m.sp500_return_pct)}</td></tr>
              <tr><td>Sharpe</td><td className="num">{m.sharpe ?? "n/a"}</td><td className="num">{m.sp500_sharpe ?? "n/a"}</td></tr>
              <tr><td>Worst drop</td><td className="num">{pct(m.max_drawdown_pct, false)}</td><td className="num">{pct(m.sp500_max_drawdown_pct, false)}</td></tr>
            </tbody>
          </table>
          <p className="small">Equal-weighted S&amp;P 500: {pct(m.sp500_equal_return_pct)}. {m.luck_note}</p>
          {m.emergency_brake && <p className="problem">Emergency brake: buys paused for a full review.</p>}
          <p className="small">Turnover {pct(m.turnover_pct, false)}, costs {pct(m.costs_pct, false)}.</p>
          <h4>Slices</h4>
          <table>
            <tbody>
              {m.slices.map((s) => (
                <tr key={s.name}>
                  <td>{s.name} ({s.positions})</td>
                  <td className="num">{pct(s.return_pct)}</td>
                  <td className="num muted">S&amp;P {pct(s.sp500_return_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4>Agent panel record</h4>
          <p className="small">
            {m.panel_record.disagreements} disagreements with the math: panel right {m.panel_record.panel_right}, math
            right {m.panel_record.math_right}. {m.panel_record.vote_earned ? "The panel has earned a vote." : "No vote yet (needs at least 30 and a clear lead)."}
          </p>
          <h4>Noise and cost</h4>
          <p className="small">
            Alerts sent: {m.alerts_sent.urgent} urgent, {m.alerts_sent.important} important, {m.alerts_sent.info} info.
            AI cost {usd(m.llm_usd)} of {usd(m.llm_cap_usd)}.
          </p>
          {m.notes.length > 0 && <ul className="list">{m.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
          <Stamp d={m} />
        </>
      )}
    </Load>
  );
}

const KINDS = ["buy", "sell", "deposit", "withdrawal"] as const;
const num = (v: FormDataEntryValue | null) => (v === null || v === "" ? null : Number(v));
const str = (v: FormDataEntryValue | null) => (v === null || v === "" ? null : String(v).trim());

export function LogScreen() {
  const [kind, setKind] = useState<(typeof KINDS)[number]>("buy");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const isTrade = kind === "buy" || kind === "sell";

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    // Exactly the fields pipeline/db.py ingest_inbox accepts.
    const trade = {
      kind,
      date: str(f.get("date")),
      symbol: isTrade ? str(f.get("symbol"))?.toUpperCase() ?? null : null,
      cik: isTrade ? num(f.get("cik")) : null,
      lot_id: isTrade ? str(f.get("lot_id")) : null,
      shares: isTrade ? num(f.get("shares")) : null,
      price: isTrade ? num(f.get("price")) : null,
      amount: isTrade ? null : num(f.get("amount")),
      fees: isTrade ? num(f.get("fees")) ?? 0 : 0,
      note: str(f.get("note")),
    };
    if (isTrade && !((trade.shares ?? 0) > 0 && (trade.price ?? 0) > 0)) {
      return setMsg({ ok: false, text: "Shares and price must be above zero." });
    }
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch("/api/trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(trade),
      });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()) || r.statusText}`);
      setMsg({ ok: true, text: "Saved to the inbox. It shows up in holdings after the next pipeline run." });
      form.reset();
    } catch (err) {
      setMsg({ ok: false, text: `Not saved: ${(err as Error).message}` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <nav className="tabs">
        <a href="#/log" className="on">Log trade</a>
        <a href="#/log/checkin">Monthly check-in</a>
      </nav>
      <h2>Record a trade or cash move</h2>
      <form onSubmit={submit}>
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            {KINDS.map((k) => <option key={k}>{k}</option>)}
          </select>
        </label>
        <label>
          Date
          <input name="date" type="date" required defaultValue={new Date().toLocaleDateString("en-CA")} />
        </label>
        {isTrade ? (
          <>
            <label>
              Symbol
              <input name="symbol" required pattern="[A-Za-z][A-Za-z0-9.\-]{0,9}" autoCapitalize="characters" />
            </label>
            <label>
              CIK (optional)
              <input name="cik" type="number" min="1" step="1" inputMode="numeric" />
            </label>
            <label>
              Lot ID {kind === "sell" ? "(the lot you sold from)" : "(optional)"}
              <input name="lot_id" required={kind === "sell"} />
            </label>
            <label>
              Shares
              <input name="shares" type="number" min="0" step="any" required inputMode="decimal" />
            </label>
            <label>
              Price per share
              <input name="price" type="number" min="0" step="any" required inputMode="decimal" />
            </label>
            <label>
              Fees
              <input name="fees" type="number" min="0" step="any" inputMode="decimal" />
            </label>
          </>
        ) : (
          <label>
            Amount (USD)
            <input name="amount" type="number" min="0" step="any" required inputMode="decimal" />
          </label>
        )}
        <label>
          Note
          <input name="note" />
        </label>
        <button disabled={busy}>{busy ? "Saving..." : "Save"}</button>
      </form>
      {msg && <p className={msg.ok ? "" : "problem"}>{msg.text}</p>}
    </>
  );
}

export function CheckinScreen() {
  return (
    <>
      <nav className="tabs">
        <a href="#/log">Log trade</a>
        <a href="#/log/checkin" className="on">Monthly check-in</a>
      </nav>
      <Load<Holdings> file="holdings">
        {(h) => (
          <>
            <h2>Does this match your broker?</h2>
            <p className="small">Compare with your latest broker statement. If something differs, log the missing trade, deposit, or withdrawal.</p>
            <table>
              <thead>
                <tr><th>Holding</th><th className="num">Shares</th><th className="num">Value</th></tr>
              </thead>
              <tbody>
                {h.holdings.map((x) => (
                  <tr key={x.cik}>
                    <td>{x.symbol}</td>
                    <td className="num">{x.shares}</td>
                    <td className="num">{usd(x.value_usd)}</td>
                  </tr>
                ))}
                <tr><td>Cash</td><td></td><td className="num">{usd(h.cash_usd)}</td></tr>
              </tbody>
            </table>
            <p><a href="#/log">Log a trade or cash move</a></p>
            <Stamp d={h} />
          </>
        )}
      </Load>
    </>
  );
}

export function SettingsScreen() {
  return (
    <>
      <Load<Settings> file="settings">
        {(s) => (
          <>
            <h2>{s.mode === "paper" ? "Paper mode" : "Real money"}, managing {usd(s.managed_usd)}</h2>
            <table>
              <tbody>
                <tr><td>Quiet hours</td><td>{s.quiet_hours.start} to {s.quiet_hours.end} ({s.quiet_hours.tz})</td></tr>
                <tr><td>Alerts by</td><td>{[s.channels.email && "email", s.channels.discord && "Discord"].filter(Boolean).join(" and ") || "none"}</td></tr>
                <tr><td>AI cost cap</td><td>{usd(s.llm_cap_usd)} a month</td></tr>
                <tr><td>Test years</td><td>{s.test_years_locked ? "locked" : "unlocked"}</td></tr>
              </tbody>
            </table>
            {s.snoozed.length > 0 && (
              <p className="small">
                Snoozed: {s.snoozed.map((z) => `${z.symbol} until ${z.until}`).join(", ")}. Urgent alerts still get through.
              </p>
            )}
            <p className="muted small">Read only here. Settings are changed in the pipeline, not on this site.</p>
          </>
        )}
      </Load>
      <Load<Health> file="health">
        {(h) => (
          <>
            <h4>App health: {h.status}</h4>
            <p className="small">{h.summary}</p>
            <ul className="list">
              {h.jobs.map((j) => (
                <li key={j.name}>
                  {j.name}: {j.ok ? "ok" : "failed"}, {j.last_run ? j.last_run.replace("T", " ") : "never run"}
                  {j.note && `. ${j.note}`}
                </li>
              ))}
              {h.checks.filter((c) => c.outcome !== "pass").map((c) => (
                <li key={c.name}>
                  {c.name} ({c.outcome}): {c.detail}
                </li>
              ))}
              {h.quarantined.map((q) => (
                <li key={q.cik}>Quarantined {q.symbol}: {q.reason}</li>
              ))}
            </ul>
            <p className="small">
              Last backup {h.last_backup?.replace("T", " ") ?? "never"}. Restore test{" "}
              {h.last_restore_test ? `${h.last_restore_test.ok ? "passed" : "failed"} on ${h.last_restore_test.date}` : "not run yet"}.
              AI spend this month {usd(h.llm_month_usd)}.
            </p>
            <Stamp d={h} />
          </>
        )}
      </Load>
    </>
  );
}
