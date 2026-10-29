import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Device } from "../api";
import { SetupPage } from "../components/setup/SetupPage";

type Call = { method: string; path: string; body: unknown };

function mockFetch(handler: (method: string, path: string, body: unknown) => { status: number; body?: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, path: input, body });
      const r = handler(method, input, body);
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const nvr: Device = {
  id: "d1", name: "Gate NVR", kind: "nvr", source_label: "NVR (real device)", host: "192.168.1.20", rtsp_port: 554,
  username: "viewer", has_password: true, main_stream: "/cam/realmonitor?channel=1&subtype=0", sub_stream: null,
  main_url_display: "rtsp://***@192.168.1.20:554/cam/realmonitor?channel=1&subtype=0", sub_url_display: null,
  status: "not_tested", status_detail: null, tested_at: null, streams: {}, editable: true, use_in_frigate: false,
};

afterEach(() => vi.unstubAllGlobals());

describe("SetupPage", () => {
  it("keeps the three source types visibly separate", async () => {
    mockFetch(() => ({ status: 200, body: [] }));
    render(<SetupPage />);
    expect(await screen.findByRole("heading", { name: "Browser webcam" })).toBeInTheDocument();
    expect(screen.getByText("Local preview only")).toBeInTheDocument();
    expect(screen.getByText(/it does not test the RTSP or engine pipeline/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Synthetic RTSP test source" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "IP cameras and NVRs" })).toBeInTheDocument();
  });

  it("shows Not Tested until the server reports a result, then Connected", async () => {
    mockFetch((method, path) => {
      if (method === "POST" && path === "/api/devices/d1/test")
        return { status: 200, body: { ...nvr, status: "connected", status_detail: "Video decoded: h264 1280x720",
          tested_at: "2026-10-22T10:00:00+00:00",
          streams: { main: { status: "connected", detail: "ok", codec: "h264", width: 1280, height: 720, fps: 15 } } } };
      return { status: 200, body: [nvr] };
    });
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    expect(within(card).getByText("Not Tested")).toBeInTheDocument();
    expect(within(card).queryByText("Connected")).not.toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(within(card).getAllByText("Connected").length).toBeGreaterThan(0));
    expect(within(card).getByText(/h264 1280×720/)).toBeInTheDocument();
  });

  it.each([
    ["unreachable", "Unreachable"],
    ["invalid_credentials", "Invalid Credentials"],
    ["reachable_unverified", "Reachable, Stream Not Verified"],
  ])("labels status %s", async (status, label) => {
    mockFetch(() => ({ status: 200, body: [{ ...nvr, status }] }));
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    expect(within(card).getByText(label)).toBeInTheDocument();
  });

  it("never displays a password, only the masked URL", async () => {
    mockFetch(() => ({ status: 200, body: [nvr] }));
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    expect(within(card).getByText(nvr.main_url_display)).toBeInTheDocument();
    expect(within(card).getByText(/password stored \(encrypted\)/)).toBeInTheDocument();
  });

  it("adds a device and shows server validation errors", async () => {
    const calls = mockFetch((method) => {
      if (method === "POST")
        return { status: 422, body: { detail: [{ loc: ["body", "host"], msg: "Value error, 127.0.0.1 is a loopback, link-local, multicast or reserved address" }] } };
      return { status: 200, body: [] };
    });
    render(<SetupPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Add camera or NVR" }));
    const form = screen.getByRole("form", { name: "Add device" });
    fireEvent.change(within(form).getByLabelText("Device name"), { target: { value: "Cam" } });
    fireEvent.change(within(form).getByLabelText("IP address or hostname"), { target: { value: "127.0.0.1" } });
    fireEvent.change(within(form).getByLabelText("Main stream path or URL"), { target: { value: "/live" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save configuration" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("host: 127.0.0.1 is a loopback");
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toMatchObject({ name: "Cam", kind: "nvr", host: "127.0.0.1", rtsp_port: 554, main_stream: "/live",
      password: null, sub_stream: null });
  });

  it("editing without typing a password keeps the stored one", async () => {
    const calls = mockFetch((method) =>
      method === "PUT" ? { status: 200, body: { ...nvr, name: "Gate NVR 2" } } : { status: 200, body: [nvr] });
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    fireEvent.click(within(card).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit device" });
    expect(within(form).getByLabelText("Password")).toHaveValue("");
    fireEvent.change(within(form).getByLabelText("Device name"), { target: { value: "Gate NVR 2" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save configuration" }));
    await screen.findByRole("article", { name: "Gate NVR 2" });
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.path).toBe("/api/devices/d1");
    expect(put.body).toMatchObject({ password: null, clear_password: false });
  });

  it("removes a device after confirmation", async () => {
    const calls = mockFetch((method) => (method === "DELETE" ? { status: 204 } : { status: 200, body: [nvr] }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    fireEvent.click(within(card).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: "Gate NVR" })).not.toBeInTheDocument());
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/devices/d1")).toBe(true);
  });

  it("offers Use in Frigate only after a successful test", async () => {
    mockFetch(() => ({ status: 200, body: [nvr] }));
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    const box = within(card).getByRole("checkbox", { name: "Use in Frigate" });
    expect(box).not.toBeChecked();
    expect(box).toBeDisabled();
    expect(within(card).getByText("Available after a successful connection test.")).toBeInTheDocument();
  });

  it("switches Use in Frigate on through the API", async () => {
    const connected = { ...nvr, status: "connected" };
    const calls = mockFetch((method) =>
      method === "PUT" ? { status: 200, body: { ...connected, use_in_frigate: true } } : { status: 200, body: [connected] });
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    const box = within(card).getByRole("checkbox", { name: "Use in Frigate" });
    expect(box).toBeEnabled();
    fireEvent.click(box);
    await waitFor(() => expect(box).toBeChecked());
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.path).toBe("/api/devices/d1/frigate");
    expect(put.body).toEqual({ use_in_frigate: true });
    expect(within(card).getByText(/Takes effect only when the configuration is applied/)).toBeInTheDocument();
  });

  it("lets an approved device that is no longer connected be switched off, and says it is skipped", async () => {
    const stale = { ...nvr, status: "unreachable", use_in_frigate: true };
    const calls = mockFetch((method) =>
      method === "PUT" ? { status: 200, body: { ...stale, use_in_frigate: false } } : { status: 200, body: [stale] });
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    expect(within(card).getByText(/Skipped when applying to Frigate/)).toBeInTheDocument();
    const box = within(card).getByRole("checkbox", { name: "Use in Frigate" });
    expect(box).toBeEnabled();
    fireEvent.click(box);
    await waitFor(() => expect(box).not.toBeChecked());
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ use_in_frigate: false });
  });

  it("shows the server's refusal and keeps the switch off", async () => {
    const connected = { ...nvr, status: "connected" };
    mockFetch((method) =>
      method === "PUT" ? { status: 409, body: { detail: "At most 32 devices can be used in Frigate" } }
        : { status: 200, body: [connected] });
    render(<SetupPage />);
    const card = await screen.findByRole("article", { name: "Gate NVR" });
    fireEvent.click(within(card).getByRole("checkbox", { name: "Use in Frigate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Gate NVR: At most 32 devices can be used in Frigate");
    expect(within(card).getByRole("checkbox", { name: "Use in Frigate" })).not.toBeChecked();
  });

  it("starts the webcam only on request, through the browser API", async () => {
    mockFetch(() => ({ status: 200, body: [] }));
    const stop = vi.fn();
    const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop }] }) as unknown as MediaStream);
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    render(<SetupPage />);
    expect(getUserMedia).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Start webcam preview" }));
    await screen.findByRole("button", { name: "Stop webcam" });
    expect(getUserMedia).toHaveBeenCalledWith({ video: true, audio: false });
    fireEvent.click(screen.getByRole("button", { name: "Stop webcam" }));
    expect(stop).toHaveBeenCalled();
  });

  it("reports a denied webcam permission", async () => {
    mockFetch(() => ({ status: 200, body: [] }));
    const denied = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn(async () => { throw denied; }) } });
    render(<SetupPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Start webcam preview" }));
    expect(await screen.findByText("Camera permission was denied in the browser.")).toBeInTheDocument();
  });
});
