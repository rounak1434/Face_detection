import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, AuditEntry, Person, PersonUpdate } from "../../api";
import { PasswordDialog } from "./PasswordDialog";
import { RecognitionPanel } from "./RecognitionPanel";

const ACCEPTED = ["image/jpeg", "image/png", "image/webp"];
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ENROLL_ERRORS: Record<string, string> = {
  no_face: "No face was found in the image.",
  multiple_faces: "More than one face was found. A reference image must show exactly one person.",
  low_quality: "The face is not usable as a reference.",
  person_inactive: "Activate this person before adding reference images.",
  too_many_faces: "This person already has the maximum number of reference images.",
};

// The operator's local date (toISOString would give the UTC date, which is a day behind or ahead
// for part of the day). The server allows one day of slack for the same reason.
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function NewPersonForm({ onCreated, onCancel }: { onCreated: (p: Person) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [consentDate, setConsentDate] = useState(today());
  const [consentNote, setConsentNote] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      onCreated(await api.createPerson({ display_name: name, consent_date: consentDate, consent_note: consentNote,
        notes: notes.trim() || null }));
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <form className="device-form" onSubmit={submit} aria-label="New authorized person">
      <p className="muted small">
        Add only people who are authorized and have consented to face recognition. Record where the consent evidence
        is kept; the form itself is not stored here.
      </p>
      <div className="form-grid">
        <label className="span-2">Name<input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} /></label>
        <label>Consent date<input type="date" value={consentDate} max={today()} onChange={(e) => setConsentDate(e.target.value)} required /></label>
        <label>Consent evidence
          <input value={consentNote} onChange={(e) => setConsentNote(e.target.value)} required maxLength={500}
            placeholder="e.g. signed form #12, HR file" />
        </label>
        <label className="span-2">Notes (optional)<input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={500} /></label>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" disabled={busy}>Add person</button>
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

function EditPersonForm({ person, onSaved, onCancel }: {
  person: Person;
  onSaved: (p: Person) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(person.display_name);
  const [consentDate, setConsentDate] = useState(person.consent.date);
  const [consentNote, setConsentNote] = useState(person.consent.note);
  const [notes, setNotes] = useState(person.notes ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    // Only what changed is sent, so the audit log lists exactly the edited fields.
    const update: PersonUpdate = {};
    if (name.trim() !== person.display_name) update.display_name = name;
    if (consentDate !== person.consent.date) update.consent_date = consentDate;
    if (consentNote.trim() !== person.consent.note) update.consent_note = consentNote;
    if ((notes.trim() || null) !== person.notes) update.notes = notes.trim() || null;
    if (Object.keys(update).length === 0) return onCancel();
    setBusy(true);
    try {
      onSaved(await api.updatePerson(person.id, update));
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <form className="device-form" onSubmit={submit} aria-label={`Edit ${person.display_name}`}>
      <div className="form-grid">
        <label className="span-2">Name<input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} /></label>
        <label>Consent date<input type="date" value={consentDate} max={today()} onChange={(e) => setConsentDate(e.target.value)} required /></label>
        <label>Consent evidence<input value={consentNote} onChange={(e) => setConsentNote(e.target.value)} required maxLength={500} /></label>
        <label className="span-2">Notes (optional)<input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={500} /></label>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" disabled={busy}>Save changes</button>
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

function PersonDetail({ id, onChanged, onDeleted }: { id: string; onChanged: () => void; onDeleted: () => void }) {
  const [person, setPerson] = useState<Person | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [enrollError, setEnrollError] = useState<{ message: string; reasons: string[] } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editing, setEditing] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setPerson(await api.person(id));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [id]);

  useEffect(() => {
    setPerson(null);
    setEnrollError(null);
    load();
  }, [load]);

  const enroll = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (fileRef.current) fileRef.current.value = "";
    if (!file) return;
    setEnrollError(null);
    if (!ACCEPTED.includes(file.type)) return setEnrollError({ message: "Choose a JPEG, PNG or WebP image.", reasons: [] });
    if (file.size > MAX_UPLOAD_BYTES) return setEnrollError({ message: "The image is larger than 10 MB.", reasons: [] });
    setUploading(true);
    try {
      await api.enrollFace(id, file);
      await load();
      onChanged();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : undefined;
      setEnrollError({
        message: (code && ENROLL_ERRORS[code]) || (err as Error).message,
        reasons: err instanceof ApiError ? err.reasons ?? [] : [],
      });
    } finally {
      setUploading(false);
    }
  };

  const setActive = async (active: boolean) => {
    try {
      setPerson(await api.updatePerson(id, { active }));
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const removeFace = async (faceId: string) => {
    if (!window.confirm("Remove this reference image? The image and its embedding are deleted permanently.")) return;
    try {
      await api.deleteReferenceFace(id, faceId);
      await load();
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (!person) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">Loading…</p>;
  const faces = person.faces ?? [];

  return (
    <article className="device person" aria-label={person.display_name}>
      <div className="device-head">
        <div>
          <h3>{person.display_name}</h3>
          <p className="muted small">
            Consent {person.consent.date} · {person.consent.note}
            {person.notes ? ` · ${person.notes}` : ""} · added by {person.created_by}
          </p>
        </div>
        <span className={`status ${person.active ? "status-ok" : "status-neutral"}`}>{person.active ? "Active" : "Inactive"}</span>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {editing && (
        <EditPersonForm person={person} onCancel={() => setEditing(false)}
          onSaved={(p) => { setPerson({ ...p, faces: p.faces ?? person.faces }); setEditing(false); onChanged(); }} />
      )}
      <ul className="crops refs">
        {faces.map((f) => (
          <li key={f.id} className="crop">
            <img src={f.image_url} alt={`Reference image of ${person.display_name}`} />
            <div className="small">
              {f.embedded ? <span className="status status-ok">Ready to match</span>
                : <span className="status status-warn" title="Embedded when recognition is switched on">Not yet embedded</span>}
              <br />face {f.box.w}×{f.box.h} px · score {f.score.toFixed(2)}
            </div>
            <button className="secondary danger" onClick={() => removeFace(f.id)}
              aria-label={`Remove reference image ${new Date(f.created_at).toLocaleString()}`}>Remove</button>
          </li>
        ))}
      </ul>
      {faces.length === 0 && <p className="muted">No reference images yet. Recognition cannot match this person until one is added.</p>}
      {enrollError && (
        <div className="error" role="alert">
          {enrollError.message}
          {enrollError.reasons.length > 0 && <ul>{enrollError.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
        </div>
      )}
      <div className="actions">
        <label className={`button file-button${!person.active || uploading ? " disabled" : ""}`}>
          {uploading ? "Checking image…" : "Add reference image"}
          <input ref={fileRef} type="file" accept={ACCEPTED.join(",")} onChange={enroll} disabled={!person.active || uploading}
            aria-label="Reference image file" />
        </label>
        <button className="secondary" onClick={() => setEditing(true)} disabled={editing}>Edit details</button>
        <button className="secondary" onClick={() => setActive(!person.active)}>
          {person.active ? "Deactivate" : "Activate"}
        </button>
        <button className="secondary danger" onClick={() => setConfirmDelete(true)}>Delete person</button>
      </div>
      <p className="muted small">
        Upload a clear photo with exactly one face, at least 80 px, not touching the edge. Images and their embeddings
        are stored encrypted.
      </p>
      {confirmDelete && (
        <PasswordDialog title={`Delete ${person.display_name}`} confirmLabel="Delete permanently" danger
          onCancel={() => setConfirmDelete(false)}
          onConfirm={async (password) => {
            await api.deletePerson(id, password);
            setConfirmDelete(false);
            onDeleted();
          }}>
          <p>
            This deletes the person, their {faces.length} reference image{faces.length === 1 ? "" : "s"} and embeddings.
            Past face events keep their result but no longer name this person. This cannot be undone.
          </p>
        </PasswordDialog>
      )}
    </article>
  );
}

function AuditLog({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (after: string | null) => {
    try {
      const page = await api.auditLog(after);
      setItems((list) => (after ? [...list, ...page.items] : page.items));
      setCursor(page.next_cursor);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load(null);
  }, [load, refreshKey]);

  return (
    <section className="card" aria-labelledby="audit-title">
      <h2 id="audit-title">Audit log</h2>
      {error && <p className="error">{error}</p>}
      {!error && items.length === 0 && <p className="muted">No changes recorded yet.</p>}
      <table className="audit">
        <tbody>
          {items.map((e) => (
            <tr key={e.id}>
              <td className="muted">{new Date(e.at).toLocaleString()}</td>
              <td>{e.username}</td>
              <td>{e.action.replace(/_/g, " ")}</td>
              <td className="muted small">{e.detail ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {cursor && <div className="actions"><button className="secondary" onClick={() => load(cursor)}>Load more</button></div>}
    </section>
  );
}

export function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [version, setVersion] = useState(0); // bumps the panel and the audit log after changes

  const load = useCallback(async () => {
    try {
      setPeople((await api.people()).items);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, version]);

  const changed = () => setVersion((v) => v + 1);

  return (
    <div className="setup">
      <h1 className="page-title">Authorized people</h1>
      <p className="notice">
        People are added by hand, with a consent record, and reference images come only from files you upload here.
        Nobody is ever enrolled from camera footage. Deleting a person needs your password.
      </p>
      <RecognitionPanel refreshKey={version} onChanged={changed} />

      <section className="card" aria-labelledby="people-title">
        <h2 id="people-title">People</h2>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && people.length === 0 && !adding && <p className="muted">No authorized people yet.</p>}
        <ul className="people-list">
          {people.map((p) => (
            <li key={p.id}>
              <button className={`capture-row person-row${selected === p.id ? " selected" : ""}`}
                onClick={() => setSelected(selected === p.id ? null : p.id)} aria-expanded={selected === p.id}>
                <span>{p.display_name}</span>
                <span className={`status ${p.active ? "status-ok" : "status-neutral"}`}>{p.active ? "Active" : "Inactive"}</span>
                <span className="muted">{p.face_count} reference image{p.face_count === 1 ? "" : "s"}</span>
                <span className="muted">consent {p.consent.date}</span>
              </button>
              {selected === p.id && (
                <PersonDetail id={p.id} onChanged={changed} onDeleted={() => { setSelected(null); changed(); }} />
              )}
            </li>
          ))}
        </ul>
        {adding ? (
          <NewPersonForm onCancel={() => setAdding(false)}
            onCreated={(p) => { setAdding(false); setSelected(p.id); changed(); }} />
        ) : (
          <div className="actions"><button onClick={() => setAdding(true)}>Add authorized person</button></div>
        )}
      </section>

      <AuditLog refreshKey={version} />
    </div>
  );
}
