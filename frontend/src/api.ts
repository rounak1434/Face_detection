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

export type AnalysedFace = {
  index: number;
  box: Box;
  score: number;
  keypoints5: [number, number][];
  crop_box: Box | null;
  crop_data_url: string | null;
};

export type FaceAnalysis = {
  source: "inference";
  image: { width: number; height: number; format: string };
  faces: AnalysedFace[];
  face_count: number;
  message: string | null;
  parameters: { min_score: number; max_faces: number; crop_margin: number } | null;
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
  notes: string;
  runtime: string;
};

export type ModelsResponse = { inference: { reachable: boolean; detail?: string }; models: ModelInfo[] };

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    let code: string | undefined;
    try {
      const body = await resp.json();
      if (typeof body.error === "string") code = body.error;
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
    throw new ApiError(resp.status, detail || `HTTP ${resp.status}`, code);
  }
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

const cam = (name: string) => `/api/cameras/${encodeURIComponent(name)}`;

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
  devices: () => request<Device[]>("/api/devices"),
  createDevice: (input: DeviceInput) =>
    request<Device>("/api/devices", { method: "POST", body: JSON.stringify(input) }),
  updateDevice: (id: string, input: DeviceInput) =>
    request<Device>(`/api/devices/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(input) }),
  deleteDevice: (id: string) => request<void>(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" }),
  testDevice: (id: string) => request<Device>(`/api/devices/${encodeURIComponent(id)}/test`, { method: "POST" }),
  addSynthetic: () => request<Device>("/api/devices/synthetic", { method: "POST" }),
  models: () => request<ModelsResponse>("/api/models"),
  // The file is sent as the raw body with its own image type.
  analyseFaces: (file: File, minScore?: number) =>
    request<FaceAnalysis>(`/api/analysis/faces${minScore !== undefined ? `?min_score=${minScore}` : ""}`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": file.type || "application/octet-stream" },
    }),
  latestFrameUrl: (camera: string) => `${cam(camera)}/latest.jpg?t=${Date.now()}`,
  liveSocketUrl: (camera: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${cam(camera)}/live`;
  },
};
