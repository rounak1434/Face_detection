import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "../App";
import { Captures } from "../components/Captures";

const renderApp = (path = "/") => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
import { supportedCodecs } from "../mse";

type Route = { status: number; body?: unknown };

function mockFetch(routes: Record<string, Route>) {
  const calls: { method: string; path: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const path = input.split("?")[0];
      calls.push({ method, path });
      const route = routes[`${method} ${path}`] ?? { status: 404, body: { detail: "not mocked" } };
      return new Response(route.body === undefined ? null : JSON.stringify(route.body), {
        status: route.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const health = { status: "ok", app: "AI Surveillance", version: "0.1.0", frigate: { reachable: true, version: "0.18.2" } };

beforeEach(() => {
  // jsdom has no MediaSource, so LiveView uses the still-frame fallback.
  vi.unstubAllGlobals();
});

afterEach(() => vi.unstubAllGlobals());

describe("supportedCodecs", () => {
  it("keeps only codecs the browser accepts", () => {
    expect(supportedCodecs((m) => m.includes("avc1"))).toBe("avc1.640029,avc1.64002A,avc1.640033");
    expect(supportedCodecs(() => false)).toBe("");
  });
});

describe("App", () => {
  it("shows the login form when not signed in", async () => {
    mockFetch({ "GET /api/health": { status: 200, body: health }, "GET /api/auth/me": { status: 401 } });
    renderApp();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "AI Surveillance" })).toBeInTheDocument();
  });

  it("lists cameras and takes a snapshot", async () => {
    const calls = mockFetch({
      "GET /api/health": { status: 200, body: health },
      "GET /api/auth/me": { status: 200, body: { username: "admin" } },
      "GET /api/cameras": {
        status: 200,
        body: [{ name: "entrance", enabled: true, detect_width: 1280, detect_height: 720, recording: true }],
      },
      "GET /api/captures": { status: 200, body: [] },
      "POST /api/cameras/entrance/snapshot": {
        status: 201,
        body: { id: "1", camera: "entrance", kind: "snapshot", status: "ready" },
      },
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "entrance" })).toBeInTheDocument();
    expect(screen.getByAltText("Latest frame from entrance")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Snapshot" }));
    expect(await screen.findByText("Snapshot saved.")).toBeInTheDocument();
    expect(calls).toContainEqual({ method: "POST", path: "/api/cameras/entrance/snapshot" });
  });
});

describe("Captures", () => {
  it("shows failure reasons and disables unavailable items", async () => {
    mockFetch({
      "GET /api/captures": {
        status: 200,
        body: [{
          id: "2", camera: "entrance", kind: "clip", status: "failed", created_at: "2026-10-22T10:00:00Z",
          size_bytes: null, duration_seconds: 10, error: "No recordings found", file_url: null,
        }],
      },
    });
    render(<Captures refreshKey={0} />);
    await waitFor(() => expect(screen.getByText("No recordings found")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Clip 10 s/ })).toBeDisabled();
  });

  const snapshot = {
    id: "s1", camera: "entrance", kind: "snapshot", status: "ready", created_at: "2026-10-22T10:00:00Z",
    size_bytes: 2000, duration_seconds: null, error: null, file_url: "/api/captures/s1/file",
  };

  it("deletes a capture after confirmation and reloads the list", async () => {
    const routes: Record<string, Route> = {
      "GET /api/captures": { status: 200, body: [snapshot] },
      "DELETE /api/captures/s1": { status: 204 },
    };
    const calls = mockFetch(routes);
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    render(<Captures refreshKey={0} />);
    const del = await screen.findByRole("button", { name: /Delete snapshot from entrance/ });
    routes["GET /api/captures"] = { status: 200, body: [] };
    fireEvent.click(del);
    expect(await screen.findByText("No snapshots or clips yet.")).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledOnce();
    expect(calls).toContainEqual({ method: "DELETE", path: "/api/captures/s1" });
  });

  it("does nothing when the deletion is cancelled", async () => {
    const calls = mockFetch({ "GET /api/captures": { status: 200, body: [snapshot] } });
    vi.stubGlobal("confirm", vi.fn(() => false));
    render(<Captures refreshKey={0} />);
    fireEvent.click(await screen.findByRole("button", { name: /Delete snapshot/ }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: /Snapshot/ })).toBeInTheDocument();
  });

  it("cannot delete a clip that is still being saved", async () => {
    mockFetch({
      "GET /api/captures": {
        status: 200,
        body: [{ ...snapshot, id: "c1", kind: "clip", status: "pending", duration_seconds: 10, file_url: null }],
      },
    });
    render(<Captures refreshKey={0} />);
    expect(await screen.findByRole("button", { name: /Delete clip from entrance/ })).toBeDisabled();
  });

  it("shows the server's reason when a deletion fails", async () => {
    mockFetch({
      "GET /api/captures": { status: 200, body: [snapshot] },
      "DELETE /api/captures/s1": { status: 500, body: { detail: "Could not delete the file: Permission denied" } },
    });
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<Captures refreshKey={0} />);
    fireEvent.click(await screen.findByRole("button", { name: /Delete snapshot/ }));
    expect(await screen.findByText("Could not delete the file: Permission denied")).toBeInTheDocument();
  });
});
