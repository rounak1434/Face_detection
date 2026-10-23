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
            {m.detail && <span className="muted small"> · {m.detail}</span>}
          </li>
        ))}
      </ul>
      <p className="muted small">
        Not available yet: recognition, verification, similarity, landmarks beyond 5 points, age and gender, expression,
        liveness.
      </p>
    </section>
  );
}
