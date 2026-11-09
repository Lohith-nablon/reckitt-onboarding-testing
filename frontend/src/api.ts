// Thin client for the FastAPI backend. Every call returns parsed JSON or throws ApiError
// with the server's "detail" message, which screens show as-is.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: form ? undefined : body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data?.detail;
    throw new ApiError(res.status, typeof d === "string" ? d : JSON.stringify(d ?? res.statusText));
  }
  return data as T;
}

export const get = <T>(p: string) => call<T>("GET", p);
export const post = <T>(p: string, b: unknown = {}) => call<T>("POST", p, b);
export const put = <T>(p: string, b: unknown) => call<T>("PUT", p, b);
export const del = <T>(p: string) => call<T>("DELETE", p);
export const upload = <T>(p: string, field: string, files: File[]) => {
  const fd = new FormData();
  files.forEach((f) => fd.append(field, f));
  return call<T>("POST", p, undefined, fd);
};
export const downloadUrl = (sid: string, key: string) =>
  `/api/sessions/${sid}/outputs/download?key=${encodeURIComponent(key)}`;

// ── shapes used across screens ────────────────────────────────────────────
export type GateStatus = "not_started" | "open" | "approved" | "reopened";
export interface Gate { status: GateStatus; decided_by: string | null; decided_at: string | null; note: string | null }
export interface SheetState {
  sheet: string | null; fingerprint: string; rows: number; columns: string[]; header_row: number;
  footer_rows: number; file_format: string; delimiter: string | null; library_hit: string | null;
}
export interface FileState {
  file_id: string; name: string; source: string; size_bytes: number; added_by: string; added_at: string;
  sheets: SheetState[]; known_layout: boolean; status: string;
}
export type Stage = "start" | "files" | "discovery" | "mapping" | "pipeline" | "harmonisation" | "validate" | "signoff" | "done";
export interface Session {
  session_id: string; name: string; market: string; retailer: string; created_by: string; created_at: string;
  updated_at: string; stage: Stage; status: "active" | "signed_off" | "archived";
  profile: Record<string, { value: string; status: string; provided_by: string }>;
  target: { source: "default" | "uploaded"; file_name: string | null; tables: Record<string, unknown> };
  files: FileState[]; gates: Record<"A" | "B" | "C", Gate>; mapping_ids: string[]; version: number;
  outputs: { base?: string; items?: OutputItem[]; library?: unknown[]; pipeline?: { confirmed_by: string; confirmed_at: string; tables: Record<string, number> } };
}
export interface OutputItem { kind: string; key?: string; uri?: string; fqn?: string; rows?: number; table?: string; file?: string }
export interface MappingSummary {
  mapping_id: string; file_id: string; file_name: string; sheet: string | null; target_table: string;
  version: number; status: string; origin: string; min_confidence: number; mapped: number; total: number;
  issues: { blocking: string[]; review: string[]; unmapped_optional: string[] };
  link_states: Record<string, number>; veritas_warnings: string[];
  published: { at: string; by: string; target_fqn: string; uris: Record<string, string>; warnings: string[] } | null;
  submission_id: string | null; veritas_match: number | null;
}
export interface JourneyStep {
  module: string; label: string; status: "done" | "in_progress" | "not_started" | "skipped"; note: string | null;
  last_event: { at: string; step: string; decided_by: string; why: string | null } | null; events: number;
}
export interface Journey {
  session_id: string; stage: Stage; status: string;
  files: { file_id: string; name: string; known_layout: boolean; status: string; steps: JourneyStep[] }[];
  gates: Record<string, Gate>; decisions: { total: number; by_actor: Record<string, number>; people: string[] };
}
export interface Overview { session: Session; journey: Journey; mappings: MappingSummary[] }
export interface TraceEvent {
  event_id: string; at: string; module: string; step: string; decided_by: string; subject: string | null;
  source: string | null; why: string | null; confidence: number | null; before: unknown; after: unknown; version: number;
}
export interface Link {
  target: string; sources: string[]; action: string | null; describe: string; value: string | null;
  parts: { column: string | null; text: string | null }[]; separator: string | null; date_format: string | null;
  target_date_format: string | null; expression: string | null; lookup: { name: string; key_columns: string[]; return_column: string } | null;
  confidence: number; rationale: string; engine: string; state: string | null; decided_by: string | null;
  decided_at: string | null; needs_review: boolean; blocking: boolean;
}
export interface Canvas extends MappingSummary {
  sources: { column: string; semantic_type: string | null; meaning: string | null; examples: string[]; used_by: string[] }[];
  targets: { column: string; dtype: string; mandatory: boolean; pk: boolean; field_source: string; description: string }[];
  links: Link[]; veritas: unknown;
}
