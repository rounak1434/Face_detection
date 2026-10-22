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
  latestFrameUrl: (camera: string) => `${cam(camera)}/latest.jpg?t=${Date.now()}`,
  liveSocketUrl: (camera: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${cam(camera)}/live`;
  },
};
