import { useEffect, useRef, useState, type ReactNode } from "react";
import { createChart, LineSeries } from "lightweight-charts";
import type { MathView, Panel, PricePoint } from "./contract";

export function useData<T>(file: string): { data?: T; error?: string } {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  useEffect(() => {
    setState({});
    fetch(`./data/${file}.json`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${r.statusText}`))))
      .then((data: T) => setState({ data }))
      .catch((e: Error) => setState({ error: `Could not load ${file}.json (${e.message}).` }));
  }, [file]);
  return state;
}

/** Renders children once the file has loaded, or says plainly why it has not. */
export function Load<T>({ file, children }: { file: string; children: (d: T) => ReactNode }) {
  const { data, error } = useData<T>(file);
  if (error) return <p className="problem">{error}</p>;
  if (!data) return <p className="muted">Loading...</p>;
  return <>{children(data)}</>;
}

export const pct = (n: number | null, sign = true) =>
  n === null ? "n/a" : `${sign && n > 0 ? "+" : ""}${n.toFixed(1)}%`;
export const usd = (n: number) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });
export const Stamp = ({ d }: { d: { as_of: string; generated_at: string } }) => (
  <p className="muted small">Data as of {d.as_of}, built {d.generated_at.replace("T", " ")}</p>
);

export function Card(props: {
  title: ReactNode;
  verdict: string;
  math: MathView;
  panel: Panel | null;
  children?: ReactNode;
}) {
  const { title, verdict, math, panel, children } = props;
  return (
    <section className="card">
      <div className="row">
        <h3>{title}</h3>
        <span className="verdict">{verdict}</span>
      </div>
      <p className="muted small">
        Score {math.score === null ? "not scored" : math.score.toFixed(0)}, {math.signals_above_60} of 7 signals above 60
        {math.news_effect !== "none" && `, news ${math.news_effect} the verdict`}
        {panel && panel.agrees !== null && `, panel ${panel.agrees ? "agrees" : "disagrees"}`}
      </p>
      {children}
      <details>
        <summary>Math</summary>
        <MathDetail math={math} />
      </details>
      <details>
        <summary>Agent panel</summary>
        <PanelDetail panel={panel} />
      </details>
    </section>
  );
}

const SIGNALS: [keyof MathView["ranks"], string][] = [
  ["momentum", "Momentum"],
  ["quality", "Quality"],
  ["value", "Value"],
  ["low_volatility", "Low volatility"],
  ["earnings_surprise", "Earnings surprise"],
  ["issuance", "Share issuance"],
  ["asset_growth", "Asset growth"],
];
const GATES: [keyof MathView["gates"], string][] = [
  ["score", "Score 80 or higher"],
  ["breadth", "5 of 7 signals above 60"],
  ["news", "News check"],
  ["timing", "Timing"],
  ["market", "Market filter"],
  ["room", "Room in the portfolio"],
];

function MathDetail({ math }: { math: MathView }) {
  return (
    <div className="detail">
      <ul>{math.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <table>
        <tbody>
          {SIGNALS.map(([k, label]) => (
            <tr key={k}>
              <td>{label}</td>
              <td className="num">{math.ranks[k] === null ? "missing" : math.ranks[k]!.toFixed(0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        {math.signals_present} of 7 signals present, {math.signals_above_60} above 60.
      </p>
      {math.missing.length > 0 && <p className="small">Missing: {math.missing.join("; ")}</p>}
      <p className="small">
        News: {math.news_effect === "none" ? "did not change the verdict" : `${math.news_effect} the verdict`}
        {math.news_note && `. ${math.news_note}`}
      </p>
      <h4>Buy rule checks</h4>
      <ul className="gates">
        {GATES.map(([k, label]) => (
          <li key={k}>
            {label}: {math.gates[k] === null ? "not checked" : math.gates[k] ? "passed" : "not passed"}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PanelDetail({ panel }: { panel: Panel | null }) {
  if (!panel) return <p className="detail muted">The panel has not reviewed this one.</p>;
  return (
    <div className="detail">
      <p>
        Panel view: <strong>{panel.verdict}</strong> (
        {panel.agrees === null ? "no comparison" : panel.agrees ? "agrees with the math" : "disagrees with the math"}),
        confidence {(panel.confidence * 100).toFixed(0)}%, {panel.date}.
      </p>
      {panel.low_confidence && <p className="small">Low confidence: a rerun on the same documents gave a different view.</p>}
      <p>{panel.summary}</p>
      {panel.claims.map((c) => (
        <div className="claim" key={c.doc_id + c.quote}>
          <p className="small">{c.side === "for" ? "For" : "Against"}: {c.text}</p>
          <blockquote>"{c.quote}"</blockquote>
          <p className="muted small">
            {c.doc_title}, {c.doc_date} ({c.tier.replace("_", " ")})
          </p>
        </div>
      ))}
      {panel.claims_removed > 0 && (
        <p className="muted small">{panel.claims_removed} claim(s) removed because the quote could not be verified.</p>
      )}
      <p className="muted small">The panel never sees the score. It advises; the math decides.</p>
    </div>
  );
}

export function PriceChart({ prices }: { prices: PricePoint[] }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#9aa3ad" },
      grid: { vertLines: { visible: false }, horzLines: { color: "#262c33" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      handleScroll: false,
      handleScale: false,
    });
    chart.addSeries(LineSeries, { color: "#7fb0e0", lineWidth: 2 }).setData(
      prices.map((p) => ({ time: p.date, value: p.close })),
    );
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [prices]);
  return <div className="chart" ref={el} />;
}
