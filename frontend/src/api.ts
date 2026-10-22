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

export class ApiError extends Error {
  constructor(public status: number, message: string) {
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
    try {
      const body = await resp.json();
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
    throw new ApiError(resp.status, detail || `HTTP ${resp.status}`);
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
  latestFrameUrl: (camera: string) => `${cam(camera)}/latest.jpg?t=${Date.now()}`,
  liveSocketUrl: (camera: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${cam(camera)}/live`;
  },
};
