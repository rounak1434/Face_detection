import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AnalysisPage } from "../components/analysis/AnalysisPage";

type Reply = { status: number; body?: unknown } | Promise<{ status: number; body?: unknown }> | "network-error";
type Call = { method: string; path: string; body: unknown; headers: Record<string, string> };

function mockFetch(handler: (method: string, path: string) => Reply) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, path: input, body: init?.body, headers: (init?.headers ?? {}) as Record<string, string> });
      const reply = handler(method, input.split("?")[0]);
      if (reply === "network-error") throw new TypeError("Failed to fetch");
      const r = await reply;
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const MODEL = { name: "yunet", task: "face_detection", version: "2023mar", status: "ready", detail: null,
  license: "MIT", license_url: "https://example.org", source: "https://example.org",
  sha256: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", notes: "", runtime: "opencv 4.14.0" };
const MODELS_READY = { inference: { reachable: true, ready: true }, models: [MODEL] };

const FACE = (index: number, x: number, extra = {}) => ({
  index, box: { x, y: 50, w: 80, h: 100 }, score: 0.93 - index * 0.1,
  keypoints5: [[x + 20, 80], [x + 60, 80], [x + 40, 100], [x + 25, 120], [x + 55, 120]],
  quality: { face_px: 80, small_face: false, touches_edge: false, sharpness: 150.2 },
  crop: { box: { x: x - 16, y: 30, w: 112, h: 140 }, width: 112, height: 140, data_url: `data:image/jpeg;base64,F${index}` },
  ...extra,
});

const RESULT = {
  status: "faces_detected", source: "inference", image: { width: 400, height: 200, format: "image/jpeg" },
  faces: [FACE(0, 100), FACE(1, 260, { quality: { face_px: 30, small_face: true, touches_edge: true, sharpness: 12.5 } })],
  face_count: 2, parameters: { min_score: 0.7, max_faces: 50, crop_margin: 0.2, small_face_px: 40 },
  model: { name: "yunet", version: "2023mar" }, processing_ms: 12.3, stored: false,
};
const NO_FACES = { ...RESULT, status: "no_faces_detected", faces: [], face_count: 0 };

function jpeg(name = "photo.jpg", size = 2048, type = "image/jpeg") {
  return new File([new Uint8Array(size)], name, { type });
}

function pick(file: File) {
  fireEvent.change(screen.getByLabelText("Image file"), { target: { files: [file] } });
}

const modelsOr = (post: Reply) => (method: string) => (method === "POST" ? post : { status: 200, body: MODELS_READY });

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:preview"), revokeObjectURL: vi.fn() }));
});
afterEach(() => vi.unstubAllGlobals());

describe("AnalysisPage", () => {
  it("shows model status, guidance and honest scope", async () => {
    mockFetch(() => ({ status: 200, body: MODELS_READY }));
    render(<AnalysisPage />);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("yunet")).toBeInTheDocument();
    expect(screen.getByText(/sha256 8f2383e4dd3c/)).toBeInTheDocument();
    expect(screen.getByText("JPEG, PNG or WebP · up to 10 MB and 40 megapixels.")).toBeInTheDocument();
    expect(screen.getByText(/not stored/)).toBeInTheDocument();
    expect(screen.getByText(/alignment keypoints, not facial landmarks/)).toBeInTheDocument();
    expect(screen.getByText(/Not implemented yet \(future phases\)/)).toBeInTheDocument();
    // No operational buttons for future features.
    for (const name of [/recogni/i, /verif/i, /liveness/i, /emotion/i]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("uploads multipart and draws boxes, keypoints, crops and metadata", async () => {
    const calls = mockFetch(modelsOr({ status: 200, body: RESULT }));
    render(<AnalysisPage />);
    await screen.findByText("Ready");
    const file = jpeg();
    pick(file);
    expect(screen.getByAltText("Uploaded image")).toHaveAttribute("src", "blob:preview");
    fireEvent.click(screen.getByRole("button", { name: "Detect faces" }));

    const box = await screen.findByLabelText("Face 1");
    expect(box).toHaveStyle({ left: "25%", top: "25%", width: "20%", height: "50%" });
    expect(screen.getByLabelText("Face 2")).toHaveStyle({ left: "65%" });
    expect(screen.getByTestId("analysis-image").querySelectorAll(".keypoint")).toHaveLength(10);
    expect(screen.getByAltText("Crop of face 1")).toHaveAttribute("src", "data:image/jpeg;base64,F0");
    expect(screen.getAllByText(/crop 112×140/)).toHaveLength(2);
    expect(screen.getByText(/detection score 0\.93/)).toBeInTheDocument();
    expect(screen.getByText("small face")).toBeInTheDocument();
    expect(screen.getByText("at image edge")).toBeInTheDocument();
    expect(screen.getByText("Real model inference")).toBeInTheDocument();
    expect(screen.getByText(/2 faces · image 400×200/)).toBeInTheDocument();

    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toBeInstanceOf(FormData);
    expect((post.body as FormData).get("file")).toBe(file);
    expect(post.headers["Content-Type"]).toBeUndefined(); // browser sets the multipart boundary
  });

  it("shows a loading state while analysing", async () => {
    let finish!: (v: { status: number; body: unknown }) => void;
    mockFetch(modelsOr(new Promise((resolve) => { finish = resolve; })));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("button", { name: "Analysing…" })).toBeDisabled();
    expect(screen.getByText("Detecting faces…")).toBeInTheDocument();
    finish({ status: 200, body: RESULT });
    expect(await screen.findByLabelText("Face 1")).toBeInTheDocument();
    expect(screen.queryByText("Detecting faces…")).not.toBeInTheDocument();
  });

  it("reports no face as a normal result, not an error", async () => {
    mockFetch(modelsOr({ status: 200, body: NO_FACES }));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByText("No face detected in this image.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Face 1")).not.toBeInTheDocument();
  });

  it("clears the image and result", async () => {
    mockFetch(modelsOr({ status: 200, body: RESULT }));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    await screen.findByLabelText("Face 1");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByAltText("Uploaded image")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Face 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Detect faces" })).toBeDisabled();
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
    mockFetch(modelsOr({ status: 400, body: { error: "invalid_image", detail: "The file could not be decoded as an image" } }));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be decoded");
  });

  it("shows a network error when the server cannot be reached", async () => {
    mockFetch(modelsOr("network-error"));
    render(<AnalysisPage />);
    pick(jpeg());
    fireEvent.click(await screen.findByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("server could not be reached");
  });

  it("says so when the model is unavailable, and shows no results", async () => {
    mockFetch((method) => (method === "POST"
      ? { status: 503, body: { error: "model_unavailable", detail: "face_detection_yunet_2023mar.onnx is not installed in the models directory",
          model_status: "missing" } }
      : { status: 200, body: { inference: { reachable: true, ready: false },
          models: [{ ...MODEL, status: "missing", detail: "face_detection_yunet_2023mar.onnx is not installed in the models directory" }] } }));
    render(<AnalysisPage />);
    expect(await screen.findByText("Not installed")).toBeInTheDocument();
    expect(screen.getByText(/no detector is loaded/)).toBeInTheDocument();
    pick(jpeg());
    fireEvent.click(screen.getByRole("button", { name: "Detect faces" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Face detection is unavailable");
    expect(screen.queryByText("Real model inference")).not.toBeInTheDocument();
    expect(screen.queryByText("No face detected in this image.")).not.toBeInTheDocument();
  });

  it("explains when the inference service is unreachable", async () => {
    mockFetch(() => ({ status: 200, body: { inference: { reachable: false, detail: "connection refused" }, models: [] } }));
    render(<AnalysisPage />);
    expect(await screen.findByRole("status")).toHaveTextContent("inference service is unreachable: connection refused");
  });
});

describe("routing", () => {
  it("opens the analysis page from its URL and keeps other pages", async () => {
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
    expect(within(nav).getByRole("link", { name: "Live" })).toHaveAttribute("href", "/live");
    expect(within(nav).getByRole("link", { name: "Camera & NVR Setup" })).toHaveAttribute("href", "/setup");
    await waitFor(() => expect(screen.getByText("AI ready")).toBeInTheDocument());
  });
});
