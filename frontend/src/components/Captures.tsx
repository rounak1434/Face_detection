import { useCallback, useEffect, useState } from "react";
import { api, Capture } from "../api";

const PENDING_POLL_MS = 3000;

function formatSize(bytes: number | null): string {
  if (bytes == null) return "";
  return bytes > 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.ceil(bytes / 1000)} kB`;
}

export function Captures({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<Capture[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Capture | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await api.captures());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const remove = async (c: Capture) => {
    const what = c.kind === "clip" ? "clip" : "snapshot";
    if (!window.confirm(`Delete this ${what} from ${c.camera}? The file is removed permanently.`)) return;
    try {
      await api.deleteCapture(c.id);
      if (open?.id === c.id) setOpen(null);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const hasPending = items.some((c) => c.status === "pending");
  useEffect(() => {
    if (!hasPending) return;
    const timer = window.setInterval(load, PENDING_POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasPending, load]);

  return (
    <section className="card">
      <h2>Captures</h2>
      {error && <p className="error">{error}</p>}
      {items.length === 0 && !error && <p className="muted">No snapshots or clips yet.</p>}
      <ul className="captures">
        {items.map((c) => (
          <li key={c.id} className="capture-item">
            <button className="capture-row" disabled={c.status !== "ready"} onClick={() => setOpen(c)}>
              <span className={`pill pill-${c.status}`}>{c.status}</span>
              <span>{c.kind === "clip" ? `Clip ${c.duration_seconds ?? ""} s` : "Snapshot"}</span>
              <span className="muted">{c.camera}</span>
              <span className="muted">{new Date(c.created_at).toLocaleString()}</span>
              <span className="muted">{c.status === "failed" ? c.error : formatSize(c.size_bytes)}</span>
            </button>
            <button
              className="secondary danger"
              disabled={c.status === "pending"}
              title={c.status === "pending" ? "Can be deleted when saving has finished" : undefined}
              aria-label={`Delete ${c.kind === "clip" ? "clip" : "snapshot"} from ${c.camera}, ${new Date(c.created_at).toLocaleString()}`}
              onClick={() => remove(c)}
            >
              Delete
            </button>
          </li>
        ))}
      </ul>
      {open?.file_url && (
        <div className="modal" role="dialog" aria-modal="true" aria-label="Capture viewer" onClick={() => setOpen(null)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            {open.kind === "clip" ? (
              <video src={open.file_url} controls autoPlay />
            ) : (
              <img src={open.file_url} alt={`Snapshot from ${open.camera}`} />
            )}
            <div className="actions">
              <a className="button" href={open.file_url} download>Download</a>
              <button onClick={() => setOpen(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
