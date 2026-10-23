import { ChangeEvent, useEffect, useState } from "react";
import { api, ApiError, FaceAnalysis, ModelsResponse } from "../../api";
import { ModelStatus } from "./ModelStatus";

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ACCEPTED = ["image/jpeg", "image/png", "image/webp"];

type State =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: FaceAnalysis }
  | { kind: "error"; message: string; code?: string };

const pct = (value: number, total: number) => `${(value / total) * 100}%`;

// Face detection and cropping on an uploaded image, using the real model in
// the inference service. Nothing shown here is simulated: without the model
// the page says so instead of showing results.
export function AnalysisPage() {
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: "idle" });

  useEffect(() => {
    api.models().then(setModels).catch(() => setModels({ inference: { reachable: false }, models: [] }));
  }, []);

  // Local preview only: the browser shows the file it already has.
  useEffect(() => {
    if (!file) return;
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

  const analyse = async () => {
    if (!file) return;
    setState({ kind: "running" });
    try {
      setState({ kind: "done", result: await api.analyseFaces(file) });
    } catch (err) {
      const e = err as ApiError;
      setState({ kind: "error", message: e.message, code: e.code });
    }
  };

  const result = state.kind === "done" ? state.result : null;

  return (
    <div className="analysis">
      <h1 className="page-title">AI analysis</h1>
      <p className="notice">
        Upload an image to detect faces with the real model running on this server. The image and the face crops
        are analysed in memory and <strong>not stored</strong>. Face detection finds faces only: it does not identify
        people and does not check liveness.
      </p>

      <ModelStatus models={models} />

      <section className="card" aria-labelledby="upload-title">
        <h2 id="upload-title">Face detection and cropping</h2>
        <div className="actions">
          <label className="button file-button">
            Choose image
            <input type="file" accept={ACCEPTED.join(",")} onChange={choose} aria-label="Image file" />
          </label>
          <button onClick={analyse} disabled={!file || state.kind === "running"}>
            {state.kind === "running" ? "Analysing…" : "Detect faces"}
          </button>
          {file && <span className="muted small">{file.name} · {(file.size / 1024).toFixed(0)} kB</span>}
        </div>

        {state.kind === "error" && (
          <p className="error" role="alert">
            {state.code === "model_unavailable" || state.code === "inference_unavailable"
              ? `Face detection is unavailable: ${state.message}`
              : state.message}
          </p>
        )}

        {preview && (
          <div className="analysis-stage">
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
              {result.image.width}×{result.image.height}
              {result.processing_ms !== null ? ` · ${result.processing_ms} ms` : ""}
              {result.parameters ? ` · score ≥ ${result.parameters.min_score}` : ""}
            </p>
            {result.face_count === 0 && <p className="muted">{result.message ?? "No face detected"}</p>}
            <ul className="crops">
              {result.faces.map((f) => (
                <li key={f.index} className="crop">
                  {f.crop_data_url && <img src={f.crop_data_url} alt={`Crop of face ${f.index + 1}`} />}
                  <div className="small">
                    <strong>Face {f.index + 1}</strong> · detection score {f.score.toFixed(2)}
                    <br />
                    box x {f.box.x}, y {f.box.y}, {f.box.w}×{f.box.h}
                  </div>
                </li>
              ))}
            </ul>
            <p className="muted small">
              The detection score is the model&apos;s confidence that the box contains a face. It says nothing about
              who the person is.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
