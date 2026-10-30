import { DeviceStatus } from "../../api";

export const STATUS_LABELS: Record<DeviceStatus, string> = {
  not_tested: "Not Tested",
  connected: "Connected",
  unreachable: "Unreachable",
  invalid_credentials: "Invalid Credentials",
  stream_not_found: "Stream Not Found",
  reachable_unverified: "Reachable, Stream Not Verified",
  address_not_allowed: "Address Not Allowed",
  error: "Error",
};

const TONE: Record<DeviceStatus, "ok" | "warn" | "bad" | "neutral"> = {
  not_tested: "neutral",
  connected: "ok",
  unreachable: "bad",
  invalid_credentials: "bad",
  stream_not_found: "bad",
  reachable_unverified: "warn",
  address_not_allowed: "bad",
  error: "bad",
};

export function StatusPill({ status, title }: { status: DeviceStatus; title?: string | null }) {
  return (
    <span className={`status status-${TONE[status]}`} title={title ?? undefined}>
      {STATUS_LABELS[status]}
    </span>
  );
}
