import { useState } from "react";
import { api, Camera } from "../api";
import { LiveView } from "./LiveView";

const CLIP_SECONDS = 10;

export function CameraTile({ camera, onCaptured }: { camera: Camera; onCaptured: () => void }) {
  const [busy, setBusy] = useState<"snapshot" | "clip" | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const run = async (kind: "snapshot" | "clip") => {
    setBusy(kind);
    setMessage(null);
    try {
      if (kind === "snapshot") {
        await api.snapshot(camera.name);
        setMessage("Snapshot saved.");
      } else {
        await api.clip(camera.name, CLIP_SECONDS);
        setMessage(`Clip of the last ${CLIP_SECONDS} s requested; it appears in Captures when ready.`);
      }
      onCaptured();
    } catch (err) {
      setMessage(`Failed: ${(err as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <article className="card camera">
      <header className="camera-head">
        <h2>{camera.name}</h2>
        <span className="muted">
          {camera.detect_width && camera.detect_height ? `${camera.detect_width}×${camera.detect_height}` : ""}
          {camera.recording ? " · recording" : " · not recording"}
        </span>
      </header>
      <LiveView camera={camera.name} />
      <div className="actions">
        <button onClick={() => run("snapshot")} disabled={busy !== null}>
          {busy === "snapshot" ? "Saving…" : "Snapshot"}
        </button>
        <button onClick={() => run("clip")} disabled={busy !== null || !camera.recording}
          title={camera.recording ? undefined : "Recording is disabled for this camera"}>
          {busy === "clip" ? "Requesting…" : `Save ${CLIP_SECONDS} s clip`}
        </button>
      </div>
      {message && <p className="message" role="status">{message}</p>}
    </article>
  );
}
