import { useEffect, useState } from "react";
import { get, Overview } from "./api";
import { ErrorBanner, Loading, useLoad } from "./components/ui";
import { JourneyPanel } from "./screens/Journey";
import { Home } from "./screens/Home";
import { StartScreen, NewSession } from "./screens/Start";
import { FilesScreen } from "./screens/Files";
import { DiscoveryScreen } from "./screens/Discovery";
import { MappingScreen } from "./screens/Mapping";
import { HarmonisationScreen } from "./screens/Harmonisation";
import { ValidateScreen } from "./screens/Validate";
import { SignoffScreen } from "./screens/Signoff";
import { PipelineScreen } from "./screens/Pipeline";

export const SCREENS = [
  { key: "start", label: "Start onboarding", stage: "start", module: "M1" },
  { key: "files", label: "Add files", stage: "files", module: "M1" },
  { key: "discovery", label: "AI Discovery", stage: "discovery", module: "M2" },
  { key: "mapping", label: "Mapping", stage: "mapping", module: "M3" },
  { key: "pipeline", label: "Pipeline", stage: "pipeline", module: "Reckitt pipeline (manual)" },
  { key: "harmonisation", label: "Harmonisation", stage: "harmonisation", module: "M4 · M5" },
  { key: "validate", label: "Validate & preview", stage: "validate", module: "M6" },
  { key: "signoff", label: "Sign off", stage: "signoff", module: "M7" },
] as const;
const ORDER = ["start", "files", "discovery", "mapping", "pipeline", "harmonisation", "validate", "signoff", "done"];

export type Nav = (path: string) => void;
export interface ScreenProps { ov: Overview; refresh: () => void; nav: Nav }

function useHash(): [string, Nav] {
  const [h, setH] = useState(window.location.hash.slice(1) || "/");
  useEffect(() => {
    const on = () => setH(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [h, (p: string) => { window.location.hash = p; }];
}

export default function App() {
  const [path, nav] = useHash();
  const parts = path.split("/").filter(Boolean);
  const sid = parts[0] === "s" ? parts[1] : null;
  const screen = parts[2] ?? "start";
  const sessions = useLoad(() => get<any[]>("/sessions"), [path]);
  const me = useLoad(() => get<{ user: string }>("/me"), []);
  const health = useLoad(() => get<{ llm: string; storage: string }>("/health"), []);
  return (
    <div className="app">
      <aside className="side">
        <div className="brand"><b>Retailer Onboarding</b><span>Reckitt POC · Nablon AI</span></div>
        <div className="list">
          <a className={`item ${!sid && parts[0] !== "new" ? "active" : ""}`} onClick={() => nav("/")}>Overview</a>
          <a className={`item ${parts[0] === "new" ? "active" : ""}`} onClick={() => nav("/new")}>+ New onboarding</a>
          <div style={{ padding: "12px 10px 4px", fontSize: 11, color: "var(--salt)", letterSpacing: ".05em" }}>SESSIONS</div>
          {(sessions.data ?? []).map((s) => (
            <a key={s.session_id} className={`item ${sid === s.session_id ? "active" : ""}`} onClick={() => nav(`/s/${s.session_id}/${s.stage === "done" ? "signoff" : s.stage === "start" ? "start" : s.stage}`)}>
              {s.name}<small>{s.retailer} · {s.market} · {s.status === "signed_off" ? "signed off" : s.stage}</small>
            </a>
          ))}
        </div>
        <div className="foot">{me.data?.user}<br />AI: {health.data?.llm ?? "…"} · storage: {health.data?.storage ?? "…"}</div>
      </aside>
      <main className="main">
        {sid ? <SessionView key={sid} sid={sid} screen={screen} nav={nav} />
          : parts[0] === "new" ? <><div className="top"><h1>New onboarding</h1><div style={{ height: 12 }} /></div><div className="body"><NewSession nav={nav} /></div></>
          : <><div className="top"><h1>Overview</h1><div style={{ height: 12 }} /></div><div className="body"><Home nav={nav} sessions={sessions.data ?? []} /></div></>}
      </main>
    </div>
  );
}

function SessionView({ sid, screen, nav }: { sid: string; screen: string; nav: Nav }) {
  const ov = useLoad(() => get<Overview>(`/sessions/${sid}`), [sid]);
  const [journeyPref, setShowJourney] = useState<boolean | null>(null);
  const showJourney = journeyPref ?? !["mapping", "discovery"].includes(screen);   // wide tables need the width
  if (ov.error) return <div className="body"><ErrorBanner error={ov.error} /></div>;
  if (!ov.data) return <Loading />;
  const s = ov.data.session;
  const reached = ORDER.indexOf(s.stage);
  const props: ScreenProps = { ov: ov.data, refresh: ov.reload, nav };
  const Screen = { start: StartScreen, files: FilesScreen, discovery: DiscoveryScreen, mapping: MappingScreen,
    pipeline: PipelineScreen, harmonisation: HarmonisationScreen, validate: ValidateScreen, signoff: SignoffScreen }[screen] ?? StartScreen;
  return (
    <>
      <div className="top">
        <div className="title">
          <div>
            <h1>{s.name}</h1>
            <div className="small muted">{s.retailer} · {s.market} · started by {s.created_by} · {s.files.length} file{s.files.length === 1 ? "" : "s"}
              {s.status === "signed_off" && <> · <span className="tag blue">Signed off</span></>}</div>
          </div>
          <button className="ghost" onClick={() => setShowJourney(!showJourney)}>{showJourney ? "Hide" : "Show"} file journey</button>
        </div>
        <nav className="steps">
          {SCREENS.map((x, i) => {
            const idx = ORDER.indexOf(x.stage);
            const enabled = idx <= Math.max(reached, 1) || (x.key === "signoff" && reached >= 6);
            return (
              <button key={x.key} disabled={!enabled} className={`step ${screen === x.key ? "active" : ""} ${idx < reached ? "done" : ""}`}
                onClick={() => nav(`/s/${sid}/${x.key}`)} title={x.module}>
                <span className="n">{i + 1}</span>{x.label}
              </button>
            );
          })}
        </nav>
      </div>
      <div className={`body ${showJourney ? "with-journey" : ""}`}>
        <div className="stack" style={{ minWidth: 0 }}><Screen {...props} /></div>
        {showJourney && <JourneyPanel ov={ov.data} />}
      </div>
    </>
  );
}
