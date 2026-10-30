// Typed wrapper around the backend API. All requests are same-origin and
// rely on the httpOnly session cookie; no token is ever stored in JS.

export type Camera = {
  name: string;
  enabled: boolean;
  detect_width: number | null;
  detect_height: number | null;
  recording: boolean;
};

export type Capture = {
  id: string;
  camera: string;
  kind: "snapshot" | "clip";
  status: "pending" | "ready" | "failed";
  created_at: string;
  size_bytes: number | null;
  duration_seconds: number | null;
  error: string | null;
  file_url: string | null;
};

export type Health = {
  status: string;
  app: string;
  version: string;
  frigate: { reachable: boolean; version?: string; error?: string };
  inference?: { reachable: boolean; models_ready?: boolean; error?: string };
};

export type DeviceKind = "ip_camera" | "nvr" | "synthetic";

export type DeviceStatus =
  | "not_tested"
  | "connected"
  | "unreachable"
  | "invalid_credentials"
  | "stream_not_found"
  | "reachable_unverified"
  | "address_not_allowed"
  | "error";

export type StreamTest = {
  status: DeviceStatus;
  detail: string;
  codec: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
};

// What the server returns: never a password or a credential-bearing URL.
export type Device = {
  id: string;
  name: string;
  kind: DeviceKind;
  source_label: string;
  host: string;
  rtsp_port: number;
  username: string | null;
  has_password: boolean;
  main_stream: string;
  sub_stream: string | null;
  main_url_display: string;
  sub_url_display: string | null;
  status: DeviceStatus;
  status_detail: string | null;
  tested_at: string | null;
  streams: Partial<Record<"main" | "sub", StreamTest>>;
  editable: boolean;
  use_in_frigate: boolean;
};

export type FrigateApplyResult = {
  cameras: { device_id: string; name: string; camera: string }[];
  skipped: { device_id: string; name: string; reason: string }[];
  restarts: number;
};

export type DeviceInput = {
  name: string;
  kind: "ip_camera" | "nvr";
  host: string;
  rtsp_port: number;
  username: string | null;
  password: string | null; // null on edit = keep the stored password
  clear_password?: boolean;
  main_stream: string;
  sub_stream: string | null;
};

export type Box = { x: number; y: number; w: number; h: number };

export type FaceQuality = {
  face_px: number;
  small_face: boolean;
  touches_edge: boolean;
  sharpness: number | null;
};

export type AnalysedFace = {
  index: number;
  box: Box;
  score: number;
  keypoints5: [number, number][];
  quality: FaceQuality | null;
  crop: { box: Box; width: number; height: number; data_url: string } | null;
};

export type FaceAnalysis = {
  status: "faces_detected" | "no_faces_detected";
  source: "inference";
  image: { width: number; height: number; format: string };
  faces: AnalysedFace[];
  face_count: number;
  parameters: { min_score: number; max_faces: number; crop_margin: number; small_face_px?: number } | null;
  model: { name: string; version: string };
  processing_ms: number | null;
  stored: boolean;
};

export type ModelInfo = {
  name: string;
  task: string;
  version: string;
  status: "ready" | "missing" | "checksum_mismatch" | "load_error" | string;
  detail: string | null;
  license: string;
  license_url: string;
  source: string;
  sha256: string | null;
  notes: string;
  runtime: string;
};

export type ModelsResponse = {
  inference: { reachable: boolean; ready?: boolean; detail?: string };
  models: ModelInfo[];
};

// --- face events, gallery, recognition (ADR-0004) ---------------------------------

export type Outcome = "AUTHORIZED" | "UNRECOGNIZED" | "INCONCLUSIVE" | "NOT_MATCHED" | "SYSTEM_ERROR";
export const OUTCOMES: Outcome[] = ["AUTHORIZED", "UNRECOGNIZED", "INCONCLUSIVE", "SYSTEM_ERROR", "NOT_MATCHED"];

export type FaceEventStatus =
  | "faces_detected"
  | "no_faces_detected"
  | "frame_unavailable"
  | "frame_rejected"
  | "inference_failed"
  | "gave_up";
export const FACE_EVENT_STATUSES: FaceEventStatus[] = [
  "faces_detected", "no_faces_detected", "frame_unavailable", "frame_rejected", "inference_failed", "gave_up",
];

export type OutcomeReason =
  | "match"
  | "below_reject_threshold"
  | "gallery_empty"
  | "between_thresholds"
  | "several_people_match"
  | "insufficient_quality"
  | "recognition_error"
  | "recognition_off"
  | "recognition_unavailable"
  | "not_performed";

export type FaceRecognition = {
  outcome: Outcome;
  reason: OutcomeReason;
  person: { id: string; display_name: string | null } | null;
  similarity: number | null;
  thresholds: { accept: number | null; reject: number | null } | null;
};

export type FaceDetectionItem = {
  id: string;
  position: number;
  box: Box;
  score: number;
  keypoints5: [number, number][];
  quality: Partial<FaceQuality>;
  crop_url: string | null;
  recognition: FaceRecognition | null; // null: stored before recognition existed
};

export type FaceEvent = {
  id: string;
  frigate_event_id: string;
  camera: string;
  started_at: string;
  ended_at: string;
  status: FaceEventStatus;
  face_count: number;
  frame_source: "recording" | "snapshot" | null;
  frame: { width: number; height: number } | null;
  frame_url: string | null;
  detector: string | null;
  detail: string | null;
  recognition_state: "off" | "unavailable" | "done" | null;
  recognizer: string | null;
  detections: FaceDetectionItem[];
};

export type FaceEventFilters = {
  camera?: string;
  status?: string;
  outcome?: string;
  person_id?: string;
  has_faces?: "true" | "false";
  since?: string; // ISO 8601
  until?: string;
};

export type Page<T> = { items: T[]; next_cursor: string | null };

export type ReferenceFace = {
  id: string;
  image: { width: number; height: number; media_type: string };
  image_url: string;
  box: Box;
  score: number;
  keypoints5: [number, number][];
  quality: Partial<FaceQuality>;
  detector: string | null;
  embedded: boolean;
  embedder: string | null;
  created_by: string;
  created_at: string;
};

export type Person = {
  id: string;
  display_name: string;
  notes: string | null;
  consent: { date: string; note: string };
  active: boolean;
  face_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  faces?: ReferenceFace[];
};

export type PersonInput = {
  display_name: string;
  consent_date: string; // YYYY-MM-DD
  consent_note: string;
  notes?: string | null;
};

export type PersonUpdate = Partial<PersonInput & { active: boolean }>;

export type RecognitionStatus = {
  enabled: boolean;
  updated_at: string | null;
  updated_by: string | null;
  embedder: { ready: boolean; model: string | null; status: string };
  gallery: { people: number; reference_faces: number; pending: number; unreadable: number | null };
  thresholds: { accept: number; reject: number; min_face_px: number };
};

export type AuditEntry = {
  id: string;
  at: string;
  username: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: string | null;
};

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string, public reasons?: string[]) {
    super(message);
  }
}

function query(params: Record<string, string | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : "";
}

// Central handling of expired or invalid sessions. Any 401 from a protected
// endpoint calls the registered handler (the app returns to the login
// screen). Only 401 does this: 403, 5xx and network errors leave the
// session alone. The auth endpoints are exempt: a wrong password at login,
// the "not signed in yet" check and logout are not session expiry.
const AUTH_EXEMPT = new Set(["/api/auth/login", "/api/auth/me", "/api/auth/logout"]);
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // FormData sets its own multipart Content-Type (with boundary); JSON otherwise.
  const defaults: Record<string, string> = init.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { ...defaults, ...((init.headers as Record<string, string>) ?? {}) },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    let code: string | undefined;
    let reasons: string[] | undefined;
    try {
      const body = await resp.json();
      if (typeof body.error === "string") code = body.error;
      if (Array.isArray(body.reasons)) reasons = body.reasons.map(String);
      if (typeof body.detail === "string") detail = body.detail;
      // FastAPI validation errors: [{loc: [..., "field"], msg: "..."}]
      else if (Array.isArray(body.detail))
        detail = body.detail
          .map((e: { loc?: unknown[]; msg?: string }) => {
            const field = e.loc && e.loc.length > 1 ? String(e.loc[e.loc.length - 1]) : "";
            const msg = (e.msg ?? "invalid").replace(/^Value error, /, "");
            return field && field !== "body" ? `${field}: ${msg}` : msg;
          })
          .join("; ");
    } catch {
      // non-JSON error body
    }
    if (resp.status === 401 && !AUTH_EXEMPT.has(path.split("?")[0])) onUnauthorized?.();
    throw new ApiError(resp.status, detail || `HTTP ${resp.status}`, code, reasons);
  }
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

const cam = (name: string) => `/api/cameras/${encodeURIComponent(name)}`;
const person = (id: string) => `/api/people/${encodeURIComponent(id)}`;
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/api/health"),
  me: () => request<{ username: string }>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<{ username: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  cameras: () => request<Camera[]>("/api/cameras"),
  snapshot: (camera: string) => request<Capture>(`${cam(camera)}/snapshot`, { method: "POST" }),
  clip: (camera: string, seconds: number) =>
    request<Capture>(`${cam(camera)}/clip`, { method: "POST", body: JSON.stringify({ seconds }) }),
  captures: () => request<Capture[]>("/api/captures"),
  deleteCapture: (id: string) => request<void>(`/api/captures/${encodeURIComponent(id)}`, { method: "DELETE" }),
  devices: () => request<Device[]>("/api/devices"),
  createDevice: (input: DeviceInput) =>
    request<Device>("/api/devices", { method: "POST", body: JSON.stringify(input) }),
  updateDevice: (id: string, input: DeviceInput) =>
    request<Device>(`/api/devices/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(input) }),
  deleteDevice: (id: string) => request<void>(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" }),
  testDevice: (id: string) => request<Device>(`/api/devices/${encodeURIComponent(id)}/test`, { method: "POST" }),
  setUseInFrigate: (id: string, useInFrigate: boolean) =>
    request<Device>(`/api/devices/${encodeURIComponent(id)}/frigate`, {
      method: "PUT",
      body: JSON.stringify({ use_in_frigate: useInFrigate }),
    }),
  addSynthetic: () => request<Device>("/api/devices/synthetic", { method: "POST" }),
  applyToFrigate: () => request<FrigateApplyResult>("/api/frigate/apply", { method: "POST" }),
  models: () => request<ModelsResponse>("/api/models"),
  // Multipart upload, field "file". The server ignores the file name and checks the bytes.
  analyseFaces: (file: File, minScore?: number) => {
    const form = new FormData();
    form.append("file", file);
    return request<FaceAnalysis>(`/api/analysis/faces${minScore !== undefined ? `?min_score=${minScore}` : ""}`, {
      method: "POST",
      body: form,
    });
  },
  faceEvents: (filters: FaceEventFilters, cursor?: string | null, limit = 25) =>
    request<Page<FaceEvent>>(`/api/face-events${query({ ...filters, cursor: cursor ?? undefined, limit: String(limit) })}`),
  deleteFaceEvent: (id: string) => request<void>(`/api/face-events/${encodeURIComponent(id)}`, { method: "DELETE" }),
  people: () => request<{ items: Person[] }>("/api/people"),
  person: (id: string) => request<Person>(person(id)),
  createPerson: (input: PersonInput) => request<Person>("/api/people", json("POST", input)),
  updatePerson: (id: string, update: PersonUpdate) => request<Person>(person(id), json("PATCH", update)),
  // Needs the signed-in user's password again.
  deletePerson: (id: string, password: string) => request<void>(person(id), json("DELETE", { password })),
  enrollFace: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ReferenceFace>(`${person(id)}/faces`, { method: "POST", body: form });
  },
  deleteReferenceFace: (id: string, faceId: string) =>
    request<void>(`${person(id)}/faces/${encodeURIComponent(faceId)}`, { method: "DELETE" }),
  recognition: () => request<RecognitionStatus>("/api/recognition"),
  setRecognition: (enabled: boolean, password: string) =>
    request<RecognitionStatus>("/api/recognition", json("PUT", { enabled, password })),
  auditLog: (cursor?: string | null, limit = 20) =>
    request<Page<AuditEntry>>(`/api/audit-log${query({ cursor: cursor ?? undefined, limit: String(limit) })}`),
  latestFrameUrl: (camera: string) => `${cam(camera)}/latest.jpg?t=${Date.now()}`,
  liveSocketUrl: (camera: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${cam(camera)}/live`;
  },
};
