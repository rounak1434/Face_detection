import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

type Reply = { status: number; body?: unknown };
type Handler = (method: string, path: string) => Reply | undefined;

const HEALTH = { status: "ok", app: "AI Surveillance", version: "0.1.0",
  frigate: { reachable: true, version: "0.18.2" }, inference: { reachable: true, models_ready: true } };
const MODELS = { inference: { reachable: true, ready: true }, models: [] };

function mockFetch(handler: Handler) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const path = input.split("?")[0];
      calls.push(`${method} ${path}`);
      const r = handler(method, path) ?? defaults(method, path);
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status, headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function defaults(method: string, path: string): Reply {
  if (path === "/api/health") return { status: 200, body: HEALTH };
  if (path === "/api/auth/me") return { status: 200, body: { username: "admin" } };
  if (path === "/api/models") return { status: 200, body: MODELS };
  if (path === "/api/captures") return { status: 200, body: [] };
  if (path === "/api/cameras") return { status: 200, body: [] };
  return { status: 404, body: { detail: `unmocked ${method} ${path}` } };
}

const renderAt = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
const expired = { status: 401, body: { detail: "Not signed in" } };

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }));
});
afterEach(() => vi.unstubAllGlobals());

describe("expired sessions", () => {
  it("Live page: a 401 returns to the login screen with a notice", async () => {
    mockFetch((_m, path) => (path === "/api/cameras" ? expired : undefined));
    renderAt("/live");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Your session has expired. Please sign in again.")).toBeInTheDocument();
    expect(screen.queryByText(/Cameras unavailable/)).not.toBeInTheDocument();
  });

  it("Analysis page: a 401 on upload returns to the login screen", async () => {
    mockFetch((method, path) => (method === "POST" && path === "/api/analysis/faces" ? expired : undefined));
    renderAt("/analysis");
    await screen.findByRole("heading", { name: "AI analysis" });
    fireEvent.change(screen.getByLabelText("Image file"), {
      target: { files: [new File([new Uint8Array(100)], "a.jpg", { type: "image/jpeg" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText(/session has expired/)).toBeInTheDocument();
  });

  it("signing in again clears the notice and restores the app", async () => {
    let expiredOnce = true;
    mockFetch((method, path) => {
      if (path === "/api/cameras" && expiredOnce) {
        expiredOnce = false;
        return expired;
      }
      if (method === "POST" && path === "/api/auth/login") return { status: 200, body: { username: "admin" } };
      return undefined;
    });
    renderAt("/live");
    await screen.findByText(/session has expired/);
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "x".repeat(12) } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument();
  });
});

describe("errors that must not log out", () => {
  it.each([
    [403, "Forbidden"],
    [500, "Internal error"],
    [503, "Engine unreachable"],
  ])("HTTP %s on the Live page keeps the session", async (status, detail) => {
    mockFetch((_m, path) => (path === "/api/cameras" ? { status, body: { detail } } : undefined));
    renderAt("/live");
    expect(await screen.findByText(`Cameras unavailable: ${detail}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("a wrong password at login shows an error, not a session-expired notice", async () => {
    const calls = mockFetch((method, path) => {
      if (path === "/api/auth/me") return { status: 401, body: { detail: "Not signed in" } };
      if (method === "POST" && path === "/api/auth/login")
        return { status: 401, body: { detail: "Invalid username or password" } };
      return undefined;
    });
    renderAt("/live");
    await screen.findByRole("button", { name: "Sign in" });
    expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument(); // first visit: no notice
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password");
    expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument();
    expect(calls.filter((c) => c === "POST /api/auth/login")).toHaveLength(1); // no retry loop
  });

  it("a server error while checking the session offers a retry instead of the login form", async () => {
    let fail = true;
    mockFetch((_m, path) => {
      if (path === "/api/auth/me" && fail) {
        fail = false;
        return { status: 502, body: { detail: "Bad gateway" } };
      }
      return undefined;
    });
    renderAt("/live");
    expect(await screen.findByRole("alert")).toHaveTextContent("Bad gateway");
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("logout still works and shows no expiry notice", async () => {
    mockFetch((method, path) => (method === "POST" && path === "/api/auth/logout" ? { status: 204 } : undefined));
    renderAt("/live");
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument();
  });
});
