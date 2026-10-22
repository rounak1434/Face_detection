import { FormEvent, useState } from "react";
import { api } from "../api";

export function Login({ appName, onSignedIn }: { appName: string; onSignedIn: (user: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await api.login(username, password);
      onSignedIn(me.username);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <main className="login">
      <form className="card" onSubmit={submit}>
        <h1>{appName}</h1>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" required />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}
