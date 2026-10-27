import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  CheckinScreen,
  HoldingScreen,
  HoldingsScreen,
  LogScreen,
  PicksScreen,
  ReportsScreen,
  SettingsScreen,
  TodayScreen,
} from "./screens";
import "./styles.css";

const NAV = [
  ["today", "Today"],
  ["picks", "Picks"],
  ["holdings", "Holdings"],
  ["reports", "Reports"],
  ["log", "Log"],
  ["settings", "Settings"],
];

function screen([page, arg]: string[]) {
  switch (page) {
    case "picks": return <PicksScreen />;
    case "holdings": return <HoldingsScreen />;
    case "holding": return /^\d+$/.test(arg ?? "") ? <HoldingScreen cik={arg} /> : <p className="problem">No such holding.</p>;
    case "reports": return <ReportsScreen which={arg ?? "weekly"} />;
    case "log": return arg === "checkin" ? <CheckinScreen /> : <LogScreen />;
    case "settings": return <SettingsScreen />;
    default: return <TodayScreen />;
  }
}

function App() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => {
      setHash(location.hash);
      window.scrollTo(0, 0);
    };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  const route = hash.replace(/^#\/?/, "").split("/");
  const section = route[0] === "holding" ? "holdings" : route[0] || "today";
  return (
    <>
      <main key={hash}>{screen(route)}</main>
      <nav className="bottom">
        {NAV.map(([id, label]) => (
          <a key={id} href={`#/${id}`} className={section === id ? "on" : ""}>
            {label}
          </a>
        ))}
      </nav>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
