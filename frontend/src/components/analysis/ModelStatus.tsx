import { ModelsResponse } from "../../api";

const LABELS: Record<string, string> = {
  ready: "Ready",
  missing: "Not installed",
  checksum_mismatch: "File failed verification",
  load_error: "Failed to load",
};

export function ModelStatus({ models }: { models: ModelsResponse | null }) {
  if (models === null) return <p className="muted small">Checking models…</p>;
  if (!models.inference.reachable) {
    return (
      <p className="error" role="status">
        The inference service is unreachable{models.inference.detail ? `: ${models.inference.detail}` : ""}. No analysis
        is possible until it runs.
      </p>
    );
  }
  return (
    <section className="card" aria-labelledby="models-title">
      <h2 id="models-title">Models</h2>
      <ul className="model-list">
        {models.models.map((m) => (
          <li key={m.name}>
            <span className={`status status-${m.status === "ready" ? "ok" : "bad"}`}>{LABELS[m.status] ?? m.status}</span>{" "}
            <strong>{m.name}</strong> {m.version} · {m.task.replace("_", " ")} ·{" "}
            <a href={m.license_url} target="_blank" rel="noreferrer">{m.license}</a> · {m.runtime}
            {m.sha256 && <span className="muted small"> · sha256 {m.sha256.slice(0, 12)}…</span>}
            {m.detail && <span className="muted small"> · {m.detail}</span>}
          </li>
        ))}
      </ul>
      {models.inference.ready === false && (
        <p className="error small">The inference service is running but no detector is loaded, so analysis will fail.</p>
      )}
      <p className="muted small">
        Not implemented yet (future phases): recognition, verification, similarity, facial landmarks, age and gender,
        expression, liveness.
      </p>
    </section>
  );
}
