import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { Gate } from "../api";

// ── data loading ─────────────────────────────────────────────────────────
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(() => {
    setLoading(true);
    fn().then((d) => { setData(d); setError(null); })
      .catch((e) => setError(String(e.message ?? e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

// ── toast + action runner ────────────────────────────────────────────────
const ToastCtx = createContext<(m: string) => void>(() => {});
export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { if (msg) { const t = setTimeout(() => setMsg(null), 4200); return () => clearTimeout(t); } }, [msg]);
  return <ToastCtx.Provider value={setMsg}>{children}{msg && <div className="toast" role="status">{msg}</div>}</ToastCtx.Provider>;
}
export const useToast = () => useContext(ToastCtx);

/** Runs an async action with a busy flag and an error message the screen can show. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  const run = useCallback(async <T,>(fn: () => Promise<T>, done?: string): Promise<T | undefined> => {
    setBusy(true); setError(null);
    try {
      const r = await fn();
      if (done) toast(done);
      return r;
    } catch (e) {
      setError(String((e as Error).message ?? e));
      return undefined;
    } finally { setBusy(false); }
  }, [toast]);
  return { busy, error, run, setError };
}

// ── small pieces ─────────────────────────────────────────────────────────
export const ragOf = (v: number | null | undefined, green = 0.9, amber = 0.7) =>
  v === null || v === undefined ? null : v >= green ? "green" : v >= amber ? "amber" : "red";

export function Conf({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined) return <span className="muted small">–</span>;
  return <span className="conf" title={`confidence ${v.toFixed(2)}`}><span className="bar"><i className={ragOf(v) ?? ""} style={{ width: `${Math.round(v * 100)}%` }} /></span>{Math.round(v * 100)}%</span>;
}

const RAG_LABEL: Record<string, string> = { green: "Green", amber: "Amber", red: "Red" };
export function Rag({ r, label }: { r: string | null | undefined; label?: string }) {
  if (!r) return <span className="muted small">–</span>;
  return <span className={`rag ${r}`}>{label ?? RAG_LABEL[r]}</span>;
}

export const pct = (v: number | null | undefined) => v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;

const STATE: Record<string, [string, string]> = {
  ai_suggested: ["AI suggested", "dashed"],
  accepted: ["Accepted", "outline-blue"],
  human_modified: ["Human modified", "blue"],
  library: ["From library", "solid"],
  unmapped: ["Unmapped", ""],
};
export function LinkState({ state }: { state: string | null }) {
  const [label, cls] = STATE[state ?? "unmapped"] ?? [state ?? "", ""];
  return <span className={`tag ${cls}`}>{label}</span>;
}

export function By({ who }: { who: string | null | undefined }) {
  if (!who) return null;
  const label = who.startsWith("ai:") ? `AI (${who.slice(3)})` : who === "rules" ? "Rules" : who === "library" ? "Library" : who;
  return <span className="tag">{label}</span>;
}

export function ErrorBanner({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <div className="banner err" role="alert"><b>Action not possible.</b><span>{error}</span></div>;
}

export function Loading() { return <div className="empty"><span className="spin" /> Loading…</div>; }

export function GateBar({ name, label, gate, children }: { name: string; label: string; gate: Gate; children?: ReactNode }) {
  const ok = gate.status === "approved";
  return (
    <div className={`gate ${ok ? "approved" : ""}`}>
      <div>
        <div className="strong">Gate {name}: {label}</div>
        <div className="small muted">
          {ok ? `Approved by ${gate.decided_by} · ${fmtTime(gate.decided_at)}${gate.note ? ` · ${gate.note}` : ""}`
            : gate.status === "reopened" ? "Reopened: something changed after approval. Review and approve again."
            : gate.status === "open" ? "Waiting for a reviewer." : "Not started yet."}
        </div>
      </div>
      <div className="row">{children}</div>
    </div>
  );
}

export function fmtTime(s: string | null | undefined) {
  if (!s) return "";
  const d = new Date(s);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function Card({ title, actions, children, flush }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className="card">
      {(title || actions) && <div className="hd"><h3>{title}</h3>{actions && <div className="row">{actions}</div>}</div>}
      <div className={`bd ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Kpi({ v, l, accent, rag }: { v: ReactNode; l: string; accent?: boolean; rag?: string | null }) {
  return <div className={`kpi ${accent ? "accent" : ""} ${rag ? `rag-${rag}` : ""}`}><div className="v">{v}</div><div className="l">{l}</div></div>;
}
