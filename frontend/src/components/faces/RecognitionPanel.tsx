import { useCallback, useEffect, useState } from "react";
import { api, ApiError, RecognitionStatus } from "../../api";
import { PasswordDialog } from "./PasswordDialog";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const REFUSED: Record<string, string> = {
  embedder_unavailable: "The face embedder (SFace) is not ready in the AI service.",
  gallery_empty: "No active person has a usable reference image yet. Enroll at least one first.",
};

// The recognition switch with every state it can be in. Off by default; changing it
// needs the password again and is written to the audit log.
export function RecognitionPanel({ refreshKey, onChanged }: { refreshKey: number; onChanged: () => void }) {
  const [status, setStatus] = useState<RecognitionStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.recognition());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const toggle = async (password: string) => {
    if (!status) return;
    try {
      setStatus(await api.setRecognition(!status.enabled, password));
      setError(null);
      setAsking(false);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setAsking(false);
        setError(`Recognition was not switched on: ${REFUSED[err.code ?? ""] ?? err.message}`);
        return;
      }
      throw err; // the dialog shows wrong-password and lockout messages
    }
  };

  const on = status?.enabled ?? false;
  const embedderLabel = !status ? "…" : status.embedder.ready ? `Ready (${status.embedder.model})`
    : `Not ready (${status.embedder.status})`;

  return (
    <section className={`card source ${on ? "source-real" : "source-synthetic"}`} aria-labelledby="recognition-title">
      <header className="source-head">
        <h2 id="recognition-title">Face recognition</h2>
        <span className={`status ${on ? "status-ok" : "status-neutral"}`} data-testid="recognition-state">
          {status ? (on ? "On" : "Off") : "…"}
        </span>
      </header>
      <p className="muted">
        While on, every face in new Frigate person events is compared with the authorized people below. A match says
        how alike two faces look to the model, not who someone is; look-alikes, photos of photos and poor light can
        mislead it, and there is <strong>no liveness check</strong>. The thresholds are starting points that must be
        checked with this camera.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {status && (
        <dl className="facts">
          <dt>Embedder</dt>
          <dd className={status.embedder.ready ? undefined : "error"}>{embedderLabel}</dd>
          <dt>Ready to match</dt>
          <dd>{plural(status.gallery.people, "active person", "active people")},{" "}
            {plural(status.gallery.reference_faces, "reference image", "reference images")}</dd>
          {status.gallery.pending > 0 && (<><dt>Not yet embedded</dt><dd>{plural(status.gallery.pending, "reference image", "reference images")} (embedded when switched on)</dd></>)}
          {!!status.gallery.unreadable && (<><dt>Unreadable</dt><dd className="error">{plural(status.gallery.unreadable, "embedding", "embeddings")} (key changed or damaged)</dd></>)}
          <dt>Thresholds</dt>
          <dd>authorized ≥ {status.thresholds.accept}, unrecognized ≤ {status.thresholds.reject}, faces ≥ {status.thresholds.min_face_px} px</dd>
          {status.updated_by && (<><dt>Last changed</dt><dd>{new Date(status.updated_at ?? "").toLocaleString()} by {status.updated_by}</dd></>)}
        </dl>
      )}
      <div className="actions">
        <button onClick={() => setAsking(true)} disabled={!status || (!on && !status.embedder.ready)}
          title={status && !on && !status.embedder.ready ? "The embedder is not ready" : undefined}>
          {on ? "Switch recognition off" : "Switch recognition on"}
        </button>
      </div>
      {asking && status && (
        <PasswordDialog title={on ? "Switch recognition off" : "Switch recognition on"}
          confirmLabel={on ? "Switch off" : "Switch on"} onConfirm={toggle} onCancel={() => setAsking(false)}>
          <p>
            {on
              ? "New face events will be analysed for faces only; nobody will be matched. Past results stay."
              : "Faces in new face events will be compared with the active authorized people. Past events are not matched."}
          </p>
        </PasswordDialog>
      )}
    </section>
  );
}
