import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { Captures } from "../components/Captures";
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
    render(<App />);
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
    render(<App />);
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
});
