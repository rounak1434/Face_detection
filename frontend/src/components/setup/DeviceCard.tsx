import { Device, StreamTest } from "../../api";
import { StatusPill } from "./StatusPill";

type Props = {
  device: Device;
  testing: boolean;
  onTest: () => void;
  onEdit?: () => void;
  onRemove: () => void;
  onUseInFrigate: (value: boolean) => void;
};

function frigateHint(device: Device) {
  const connected = device.status === "connected";
  if (device.use_in_frigate && !connected) return "Skipped when applying to Frigate until a connection test passes again.";
  if (!device.use_in_frigate && !connected) return "Available after a successful connection test.";
  return "Takes effect only when the configuration is applied to Frigate.";
}

function streamLine(label: string, url: string, test?: StreamTest) {
  return (
    <div className="stream-line">
      <span className="muted">{label}</span>
      <code>{url}</code>
      {test && <StatusPill status={test.status} title={test.detail} />}
      {test?.status === "connected" && (
        <span className="muted small">
          {test.codec} {test.width}×{test.height}
          {test.fps ? ` @ ${test.fps} fps` : ""}
        </span>
      )}
    </div>
  );
}

export function DeviceCard({ device, testing, onTest, onEdit, onRemove, onUseInFrigate }: Props) {
  const canSwitchOn = device.status === "connected";
  return (
    <article className="device" aria-label={device.name}>
      <header className="device-head">
        <div>
          <h3>{device.name}</h3>
          <span className={`source-tag tag-${device.kind}`}>{device.source_label}</span>
        </div>
        <StatusPill status={device.status} title={device.status_detail} />
      </header>
      {streamLine("Main", device.main_url_display, device.streams.main)}
      {device.sub_url_display && streamLine("Sub", device.sub_url_display, device.streams.sub)}
      <p className="muted small">
        {device.username ? `User ${device.username}` : "No username"} ·{" "}
        {device.has_password ? "password stored (encrypted)" : "no password"} ·{" "}
        {device.tested_at ? `last tested ${new Date(device.tested_at).toLocaleString()}` : "never tested"}
      </p>
      {device.status_detail && <p className="small detail">{device.status_detail}</p>}
      <div className="frigate-use">
        <label>
          <input type="checkbox" checked={device.use_in_frigate}
            disabled={!device.use_in_frigate && !canSwitchOn}
            onChange={(e) => onUseInFrigate(e.target.checked)} />{" "}
          Use in Frigate
        </label>
        <span className="muted small">{frigateHint(device)}</span>
      </div>
      <div className="actions">
        <button onClick={onTest} disabled={testing}>{testing ? "Testing…" : "Test connection"}</button>
        {onEdit && <button className="secondary" onClick={onEdit}>Edit</button>}
        <button className="secondary danger" onClick={onRemove}>Remove</button>
      </div>
    </article>
  );
}
