import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  api, FACE_EVENT_STATUSES, FaceDetectionItem, FaceEvent, FaceEventFilters, FaceEventStatus, Outcome, OUTCOMES,
  Person,
} from "../../api";
import {
  formatSimilarity, OUTCOME_CLASS, OUTCOME_HELP, OUTCOME_LABEL, pct, recognitionStateLabel, STATUS_LABEL,
} from "./labels";

const PAGE_SIZE = 25;

// datetime-local gives local time without a zone; the API wants ISO 8601 (UTC when no offset).
const toIso = (local: string) => (local ? new Date(local).toISOString() : undefined);

function OutcomeBadge({ d }: { d: FaceDetectionItem }) {
  if (!d.recognition) return <span className="status status-neutral" title="Stored before recognition existed">No result</span>;
  const r = d.recognition;
  const who = r.outcome === "AUTHORIZED" ? (r.person ? r.person.display_name ?? "?" : "deleted person") : null;
  return (
    <span className={`status ${OUTCOME_CLASS[r.outcome]}`} title={OUTCOME_HELP[r.outcome]}>
      {OUTCOME_LABEL[r.outcome]}{who ? `: ${who}` : ""}
    </span>
  );
}

function EventDetail({ event, onClose, onDeleted }: { event: FaceEvent; onClose: () => void; onDeleted: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const remove = async () => {
    if (!window.confirm("Delete this face event? Its frame and face crops are removed permanently.")) return;
    setDeleting(true);
    try {
      await api.deleteFaceEvent(event.id);
      onDeleted();
    } catch (err) {
      setError((err as Error).message);
      setDeleting(false);
    }
  };

  const frame = event.frame;
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Face event" onClick={onClose}>
      <div className="modal-body event-detail" onClick={(e) => e.stopPropagation()}>
        <h2>
          {event.camera} · {new Date(event.started_at).toLocaleString()}
        </h2>
        <p className="muted small">
          {STATUS_LABEL[event.status]} · {recognitionStateLabel(event.recognition_state)}
          {event.recognizer ? ` (${event.recognizer})` : ""} · frame from {event.frame_source ?? "—"}
          {event.detail ? ` · ${event.detail}` : ""}
        </p>
        {error && <p className="error" role="alert">{error}</p>}
        {event.frame_url && frame ? (
          <div className="analysis-image" data-testid="event-frame">
            <img src={event.frame_url} alt={`Frame from ${event.camera}`} />
            {event.detections.map((d) => (
              <div key={d.id} className={`face-box face-${d.recognition?.outcome ?? "none"}`}
                aria-label={`Face ${d.position + 1}`}
                style={{ left: pct(d.box.x, frame.width), top: pct(d.box.y, frame.height),
                  width: pct(d.box.w, frame.width), height: pct(d.box.h, frame.height) }}>
                <span className="face-label">{d.position + 1}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">No frame was kept for this event (frames are kept only when a face was found).</p>
        )}
        {event.detections.length > 0 && (
          <ul className="crops">
            {event.detections.map((d) => (
              <li key={d.id} className="crop">
                {d.crop_url ? <img src={d.crop_url} alt={`Face ${d.position + 1}`} /> : <p className="muted small">No crop</p>}
                <div className="small">
                  <strong>Face {d.position + 1}</strong> <OutcomeBadge d={d} />
                  <br />
                  similarity {formatSimilarity(d.recognition?.similarity)}
                  {d.recognition?.thresholds?.accept != null &&
                    ` (accept ≥ ${d.recognition.thresholds.accept}, reject ≤ ${d.recognition.thresholds.reject})`}
                  <br />
                  detection score {d.score.toFixed(2)} · {d.box.w}×{d.box.h} px
                </div>
              </li>
            ))}
          </ul>
        )}
        <p className="muted small">
          A similarity says how alike two faces look to the model; it is not proof of identity, and there is no
          liveness check.
        </p>
        <div className="actions">
          <button className="secondary danger" onClick={remove} disabled={deleting}>Delete event</button>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

type Draft = { camera: string; status: string; outcome: string; person_id: string; has_faces: string; since: string; until: string };
const EMPTY: Draft = { camera: "", status: "", outcome: "", person_id: "", has_faces: "", since: "", until: "" };

export function FaceEventsPage() {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [filters, setFilters] = useState<FaceEventFilters>({});
  const [items, setItems] = useState<FaceEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [open, setOpen] = useState<FaceEvent | null>(null);

  useEffect(() => {
    api.people().then((r) => setPeople(r.items)).catch(() => setPeople([]));
  }, []);

  const load = useCallback(async (f: FaceEventFilters, after: string | null) => {
    setLoading(true);
    try {
      const page = await api.faceEvents(f, after, PAGE_SIZE);
      setItems((list) => (after ? [...list, ...page.items] : page.items));
      setCursor(page.next_cursor);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(filters, null);
  }, [filters, load]);

  const apply = (e: FormEvent) => {
    e.preventDefault();
    setFilters({
      camera: draft.camera.trim() || undefined, status: draft.status || undefined,
      outcome: draft.outcome || undefined, person_id: draft.person_id || undefined,
      has_faces: (draft.has_faces || undefined) as FaceEventFilters["has_faces"],
      since: toIso(draft.since), until: toIso(draft.until),
    });
  };
  const reset = () => {
    setDraft(EMPTY);
    setFilters({});
  };
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setDraft((d) => ({ ...d, [k]: e.target.value }));
  const filtered = Object.values(filters).some(Boolean);

  return (
    <div className="setup">
      <h1 className="page-title">Face events</h1>
      <p className="notice">
        Each finished person event from Frigate is checked once for faces. While recognition is switched on (People
        page), every face is also compared with the authorized people. Frames and crops are kept for 30 days by
        default.
      </p>

      <form className="card filters" onSubmit={apply} aria-label="Filters">
        <label>Camera<input value={draft.camera} onChange={set("camera")} placeholder="any" /></label>
        <label>Status
          <select value={draft.status} onChange={set("status")}>
            <option value="">any</option>
            {FACE_EVENT_STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s as FaceEventStatus]}</option>)}
          </select>
        </label>
        <label>Outcome
          <select value={draft.outcome} onChange={set("outcome")}>
            <option value="">any</option>
            {OUTCOMES.map((o) => <option key={o} value={o}>{OUTCOME_LABEL[o as Outcome]}</option>)}
          </select>
        </label>
        <label>Person
          <select value={draft.person_id} onChange={set("person_id")}>
            <option value="">anyone</option>
            {people.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
          </select>
        </label>
        <label>Faces
          <select value={draft.has_faces} onChange={set("has_faces")}>
            <option value="">any</option>
            <option value="true">with faces</option>
            <option value="false">without faces</option>
          </select>
        </label>
        <label>From<input type="datetime-local" value={draft.since} onChange={set("since")} /></label>
        <label>Until<input type="datetime-local" value={draft.until} onChange={set("until")} /></label>
        <div className="actions">
          <button type="submit">Apply filters</button>
          <button type="button" className="secondary" onClick={reset} disabled={!filtered}>Clear</button>
        </div>
      </form>

      {error && <p className="error" role="alert">Face events unavailable: {error}</p>}

      <section className="card" aria-label="Face event list">
        {!error && !loading && items.length === 0 && (
          <p className="muted">{filtered ? "No face events match these filters." : "No face events yet."}</p>
        )}
        <ul className="captures">
          {items.map((e) => (
            <li key={e.id} className="capture-item">
              <button className="capture-row event-row" onClick={() => setOpen(e)}
                aria-label={`Face event ${e.camera} ${new Date(e.started_at).toLocaleString()}`}>
                <span className="muted">{new Date(e.started_at).toLocaleString()}</span>
                <span>{e.camera}</span>
                <span>{STATUS_LABEL[e.status]}{e.face_count ? ` · ${e.face_count}` : ""}</span>
                <span className="outcomes">
                  {e.detections.length > 0
                    ? e.detections.map((d) => <OutcomeBadge key={d.id} d={d} />)
                    : <span className="muted small">{recognitionStateLabel(e.recognition_state)}</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {loading && <p className="muted" role="status">Loading…</p>}
        {cursor && !loading && (
          <div className="actions">
            <button className="secondary" onClick={() => load(filters, cursor)}>Load more</button>
          </div>
        )}
      </section>

      {open && (
        <EventDetail event={open} onClose={() => setOpen(null)}
          onDeleted={() => {
            setItems((list) => list.filter((x) => x.id !== open.id));
            setOpen(null);
          }} />
      )}
    </div>
  );
}
