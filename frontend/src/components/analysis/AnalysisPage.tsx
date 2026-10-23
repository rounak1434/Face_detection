import { ChangeEvent, useEffect, useRef, useState } from "react";
import { api, ApiError, FaceAnalysis, ModelsResponse } from "../../api";
import { ModelStatus } from "./ModelStatus";

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ACCEPTED = ["image/jpeg", "image/png", "image/webp"];
const UNAVAILABLE = new Set(["model_unavailable", "inference_unavailable"]);

type State =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: FaceAnalysis }
  | { kind: "error"; message: string; code?: string };

const pct = (value: number, total: number) => `${(value / total) * 100}%`;

// Face detection and cropping on an uploaded image, using the real model in
// the inference service. Nothing here is simulated: without the model the
// page says so instead of showing results.
export function AnalysisPage() {
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: "idle" });
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.models().then(setModels).catch(() => setModels({ inference: { reachable: false }, models: [] }));
  }, []);

  // Local preview only: the browser shows the file it already has.
  useEffect(() => {
    if (!file) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const choose = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0] ?? null;
    setState({ kind: "idle" });
    if (!picked) return setFile(null);
    if (!ACCEPTED.includes(picked.type)) {
      setFile(null);
      return setState({ kind: "error", message: "Choose a JPEG, PNG or WebP image.", code: "unsupported_format" });
    }
    if (picked.size > MAX_UPLOAD_BYTES) {
      setFile(null);
      return setState({ kind: "error", message: "The image is larger than 10 MB.", code: "too_large" });
    }
    setFile(picked);
  };

  const clear = () => {
    setFile(null);
    setState({ kind: "idle" });
    if (inputRef.current) inputRef.current.value = "";
  };

  const analyse = async () => {
    if (!file) return;
    setState({ kind: "running" });
    try {
      setState({ kind: "done", result: await api.analyseFaces(file) });
    } catch (err) {
      if (err instanceof ApiError) setState({ kind: "error", message: err.message, code: err.code });
      else setState({ kind: "error", message: "The server could not be reached. Check your connection.", code: "network" });
    }
  };

  const result = state.kind === "done" ? state.result : null;

  return (
    <div className="analysis">
      <h1 className="page-title">AI analysis</h1>
      <p className="notice">
        Upload an image to detect faces with the real model running on this server. The image and the face crops
        are analysed in memory and <strong>not stored</strong>. Face detection finds faces only: it does not identify
        people and does not check liveness. The five points per face are alignment keypoints, not facial landmarks.
      </p>

      <ModelStatus models={models} />

      <section className="card" aria-labelledby="upload-title">
        <h2 id="upload-title">Face detection and cropping</h2>
        <p className="muted small">JPEG, PNG or WebP · up to 10 MB and 40 megapixels.</p>
        <div className="actions">
          <label className="button file-button">
            Choose image
            <input ref={inputRef} type="file" accept={ACCEPTED.join(",")} onChange={choose} aria-label="Image file" />
          </label>
          <button onClick={analyse} disabled={!file || state.kind === "running"}>
            {state.kind === "running" ? "Analysing…" : "Detect faces"}
          </button>
          <button className="secondary" onClick={clear} disabled={!file && state.kind === "idle"}>Clear</button>
          {file && <span className="muted small">{file.name} · {(file.size / 1024).toFixed(0)} kB</span>}
        </div>

        {state.kind === "running" && <p className="muted" role="status">Detecting faces…</p>}

        {state.kind === "error" && (
          <p className="error" role="alert">
            {state.code && UNAVAILABLE.has(state.code)
              ? `Face detection is unavailable: ${state.message}`
              : state.message}
          </p>
        )}

        {preview && (
          <div className="analysis-stage">
            {/* Boxes are positioned in % of the image, so they stay aligned at any display size. */}
            <div className="analysis-image" data-testid="analysis-image">
              <img src={preview} alt="Uploaded image" />
              {result?.faces.map((f) => (
                <div key={f.index} className="face-box" aria-label={`Face ${f.index + 1}`}
                  style={{
                    left: pct(f.box.x, result.image.width), top: pct(f.box.y, result.image.height),
                    width: pct(f.box.w, result.image.width), height: pct(f.box.h, result.image.height),
                  }}>
                  <span className="face-label">{f.index + 1}</span>
                </div>
              ))}
              {result?.faces.flatMap((f) =>
                f.keypoints5.map(([x, y], i) => (
                  <span key={`${f.index}-${i}`} className="keypoint" aria-hidden="true"
                    style={{ left: pct(x, result.image.width), top: pct(y, result.image.height) }} />
                )),
              )}
            </div>
          </div>
        )}

        {result && (
          <div className="analysis-results" aria-live="polite">
            <p className="small">
              <span className="source-tag tag-real">Real model inference</span>{" "}
              {result.model.name} {result.model.version} · {result.face_count} face{result.face_count === 1 ? "" : "s"} ·{" "}
              image {result.image.width}×{result.image.height}
              {result.processing_ms !== null ? ` · ${result.processing_ms} ms` : ""}
              {result.parameters ? ` · score ≥ ${result.parameters.min_score}` : ""}
            </p>
            {result.status === "no_faces_detected" && (
              <p className="muted" role="status">No face detected in this image.</p>
            )}
            <ul className="crops">
              {result.faces.map((f) => (
                <li key={f.index} className="crop">
                  {f.crop ? (
                    <img src={f.crop.data_url} alt={`Crop of face ${f.index + 1}`} />
                  ) : (
                    <p className="muted small">Crop unavailable</p>
                  )}
                  <div className="small">
                    <strong>Face {f.index + 1}</strong> · detection score {f.score.toFixed(2)}
                    <br />
                    box {f.box.w}×{f.box.h} at {f.box.x},{f.box.y}
                    {f.crop && <><br />crop {f.crop.width}×{f.crop.height}</>}
                    {f.quality && (
                      <>
                        <br />
                        {f.quality.small_face && <span className="pill pill-pending">small face</span>}{" "}
                        {f.quality.touches_edge && <span className="pill pill-pending">at image edge</span>}{" "}
                        {f.quality.sharpness !== null && <span className="muted">sharpness {f.quality.sharpness}</span>}
                      </>
                    )}
                  </div>
                </li>
              ))}
            </ul>
            <p className="muted small">
              The detection score is the model&apos;s confidence that the box contains a face; it says nothing about
              who the person is. Sharpness is a simple heuristic, not a quality model.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
