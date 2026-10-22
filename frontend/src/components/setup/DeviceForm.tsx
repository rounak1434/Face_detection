import { FormEvent, useState } from "react";
import { Device, DeviceInput } from "../../api";

type Props = {
  initial?: Device;
  onSubmit: (input: DeviceInput) => Promise<void>;
  onCancel: () => void;
};

// Add/edit form for a real IP camera or NVR. The password field is
// write-only: when editing, leaving it empty keeps the stored password.
export function DeviceForm({ initial, onSubmit, onCancel }: Props) {
  const editing = !!initial;
  const [name, setName] = useState(initial?.name ?? "");
  const [kind, setKind] = useState<"ip_camera" | "nvr">(initial?.kind === "ip_camera" ? "ip_camera" : "nvr");
  const [host, setHost] = useState(initial?.host ?? "");
  const [port, setPort] = useState(String(initial?.rtsp_port ?? 554));
  const [username, setUsername] = useState(initial?.username ?? "");
  const [password, setPassword] = useState("");
  const [clearPassword, setClearPassword] = useState(false);
  const [mainStream, setMainStream] = useState(initial?.main_stream ?? "");
  const [subStream, setSubStream] = useState(initial?.sub_stream ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const portNumber = Number(port);
    if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) {
      setError("RTSP port must be a whole number from 1 to 65535.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        kind,
        host: host.trim(),
        rtsp_port: portNumber,
        username: username.trim() || null,
        password: password ? password : null,
        clear_password: editing && clearPassword,
        main_stream: mainStream.trim(),
        sub_stream: subStream.trim() || null,
      });
      setPassword("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="device-form" onSubmit={submit} aria-label={editing ? "Edit device" : "Add device"}>
      <div className="form-grid">
        <label>
          Device name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} required />
        </label>
        <label>
          Device type
          <select value={kind} onChange={(e) => setKind(e.target.value as "ip_camera" | "nvr")}>
            <option value="nvr">NVR</option>
            <option value="ip_camera">IP camera</option>
          </select>
        </label>
        <label>
          IP address or hostname
          <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.1.20" required
            autoComplete="off" />
        </label>
        <label>
          RTSP port
          <input value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" required />
        </label>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" maxLength={64} />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password" maxLength={128} disabled={clearPassword}
            placeholder={editing && initial?.has_password ? "Stored — leave empty to keep" : ""} />
        </label>
        <label className="span-2">
          Main stream path or URL
          <input value={mainStream} onChange={(e) => setMainStream(e.target.value)}
            placeholder="/cam/realmonitor?channel=1&subtype=0" required autoComplete="off" />
        </label>
        <label className="span-2">
          Substream path or URL (optional)
          <input value={subStream} onChange={(e) => setSubStream(e.target.value)}
            placeholder="/cam/realmonitor?channel=1&subtype=1" autoComplete="off" />
        </label>
      </div>
      <p className="muted small">
        Enter credentials only in the username and password fields, not inside the URL. A full rtsp:// URL is
        accepted only if it uses the same host and port as above. The path shown is a common format; check your
        device&apos;s manual.
      </p>
      {editing && initial?.has_password && (
        <label className="checkbox">
          <input type="checkbox" checked={clearPassword} onChange={(e) => setClearPassword(e.target.checked)} />
          Remove the stored password
        </label>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save configuration"}</button>
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}
