import { FormEvent, ReactNode, useState } from "react";
import { ApiError } from "../../api";

// Asks for the signed-in user's password before a sensitive action (deleting a person,
// switching recognition). The password goes only to that one request and is not kept.
export function PasswordDialog({ title, children, confirmLabel, danger, onConfirm, onCancel }: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: (password: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onConfirm(password);
    } catch (err) {
      setPassword("");
      if (err instanceof ApiError && err.status === 403) setError("Password is incorrect.");
      else if (err instanceof ApiError && err.status === 429) setError("Too many failed attempts. Try again later.");
      else setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label={title} onClick={onCancel}>
      <form className="modal-body dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>{title}</h2>
        {children}
        <label className="field">
          Your password
          <input type="password" autoComplete="current-password" value={password} autoFocus
            onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button type="submit" className={danger ? "danger-solid" : undefined} disabled={busy || !password}>
            {confirmLabel}
          </button>
          <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button>
        </div>
      </form>
    </div>
  );
}
