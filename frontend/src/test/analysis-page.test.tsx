import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AnalysisPage } from "../components/analysis/AnalysisPage";

type Reply = { status: number; body?: unknown };
type Call = { method: string; path: string; body: unknown; contentType: string | null };

function mockFetch(handler: (method: string, path: string) => Reply) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ method, path: input, body: init?.body, contentType: headers["Content-Type"] ?? null });
      const r = handler(method, input.split("?")[0]);
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const MODELS_READY = {
  inference: { reachable: true },
  models: [{ name: "yunet", task: "face_detection", version: "2023mar", status: "ready", detail: null, license: "MIT",
    license_url: "https://example.org", source: "https://example.org", notes: "", runtime: "opencv 4.14.0" }],
};

const RESULT = {
  source: "inference", image: { width: 400, height: 200, format: "image/jpeg" },
  faces: [{ index: 0, box: { x: 100, y: 50, w: 80, h: 100 }, score: 0.93,
    keypoints5: [[120, 80], [160, 80], [140, 100], [125, 120], [155, 120]],
    crop_box: { x: 84, y: 30, w: 112, h: 140 }, crop_data_url: "data:image/jpeg;base64,AAAA" }],
  face_count: 1, message: null, parameters: { min_score: 0.7, max_faces: 50, crop_margin: 0.2 },
  model: { name: "yunet", version: "2023mar" }, processing_ms: 12.3, stored: false,
};

function jpeg(name = "photo.jpg", size = 2048, type = "image/jpeg") {
  return new File([new Uint8Array(size)], name, { type });
}

function pick(file: File) {
  fireEvent.change(screen.getByLabelText("Image file"), { target: { files: [file] } });
}

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:preview"), revokeObjectURL: vi.fn() }));
});
afterEach(() => vi.unstubAllGlobals());

describe("AnalysisPage", () => {
  it("shows model status and the honest scope", async () => {
    mockFetch(() => ({ status: 200, body: MODELS_READY }));
    render(<AnalysisPage />);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("yunet")).toBeInTheDocument();
    expect(screen.getByText(/not stored/)).toBeInTheDocument();
    expect(screen.getByText(/does not identify\s+people and does not check liveness/)).toBeInTheDocument();
    expect(screen.getByText(/Not available yet: recognition/)).toBeInTheDocument();
  });

  it("uploads the raw file and draws boxes, keypoints and crops", async () => {
    const calls = mockFetch((method, path) =>
      method === "POST" && path === "/api/analysis/faces" ? { status: 200, body: RESULT } : { status: 200, body: MODELS_READY });
    render(<AnalysisPage />);
    await screen.findByText("Ready");
    const file = jpeg();
    pick(file);
    fireEvent.click(screen.getByRole("button", { name: "Detect faces" }));

    const box = await screen.findByLabelText("Face 1");
    expect(box).toHaveStyle({ left: "25%", top: "25%", width: "20%", height: "50%" });
    expect(screen.getByAltText("Crop of face 1")).toHaveAttribute("src", "data:image/jpeg;base64,AAAA");
    expect(screen.getByText("Real model inference")).toBeInTheDocument();
    expect(screen.getByText(/detection score 0\.93/)).toBeInTheDocument();
    expect(screen.getByTestId("analysis-image").querySelectorAll(".keypoint")).toHaveLength(5);

    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toBe(file);
    expect(post.contentType).toBe("image/jpeg");
  });

  it("reports no face as a normal result", async () => {
    mockFetch((method) => (method === "POST"
      ? { status: 200, body: { ...RESULT, faces: [], face_count: 0, message: "No face detected" } }
      : { status: 200, body: MODELS_READY }));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByText("No face detected")).toBeInTheDocument();
    expect(screen.queryByLabelText("Face 1")).not.toBeInTheDocument();
  });

  it("rejects unsupported and oversized files before uploading", async () => {
    const calls = mockFetch(() => ({ status: 200, body: MODELS_READY }));
    render(<AnalysisPage />);
    await screen.findByText("Ready");
    pick(jpeg("anim.gif", 100, "image/gif"));
    expect(await screen.findByRole("alert")).toHaveTextContent("JPEG, PNG or WebP");
    pick(jpeg("huge.jpg", 10 * 1024 * 1024 + 1));
    expect(await screen.findByRole("alert")).toHaveTextContent("larger than 10 MB");
    expect(screen.getByRole("button", { name: "Detect faces" })).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("shows server-side validation errors", async () => {
    mockFetch((method) => (method === "POST"
      ? { status: 400, body: { error: "invalid_image", detail: "The file could not be decoded as an image" } }
      : { status: 200, body: MODELS_READY }));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be decoded");
  });

  it("says so when the model is unavailable, and shows no results", async () => {
    mockFetch((method) => (method === "POST"
      ? { status: 503, body: { error: "model_unavailable", detail: "face_detection_yunet_2023mar.onnx not found" } }
      : { status: 200, body: { ...MODELS_READY, models: [{ ...MODELS_READY.models[0], status: "missing",
          detail: "face_detection_yunet_2023mar.onnx not found" }] } }));
    render(<AnalysisPage />);
    expect(await screen.findByText("Not installed")).toBeInTheDocument();
    pick(jpeg());
    fireEvent.click(screen.getByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Face detection is unavailable");
    expect(screen.queryByText("Real model inference")).not.toBeInTheDocument();
  });

  it("explains when the inference service is unreachable", async () => {
    mockFetch(() => ({ status: 200, body: { inference: { reachable: false, detail: "connection refused" }, models: [] } }));
    render(<AnalysisPage />);
    expect(await screen.findByRole("status")).toHaveTextContent("inference service is unreachable: connection refused");
  });
});

describe("routing", () => {
  it("opens the analysis page from its URL", async () => {
    mockFetch((_m, path) => {
      if (path === "/api/auth/me") return { status: 200, body: { username: "admin" } };
      if (path === "/api/health")
        return { status: 200, body: { status: "ok", app: "AI Surveillance", version: "0.1.0",
          frigate: { reachable: true, version: "0.18.2" }, inference: { reachable: true, models_ready: true } } };
      return { status: 200, body: MODELS_READY };
    });
    render(<MemoryRouter initialEntries={["/analysis"]}><App /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "AI analysis" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect(within(nav).getByRole("link", { name: "AI analysis" })).toHaveAttribute("aria-current", "page");
    await waitFor(() => expect(screen.getByText("AI ready")).toBeInTheDocument());
  });
});
