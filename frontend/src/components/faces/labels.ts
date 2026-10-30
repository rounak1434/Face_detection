import { FaceEvent, FaceEventStatus, Outcome, OutcomeReason } from "../../api";

// Operator-facing wording for face events and recognition. Kept in one place so the
// events page, the people page and the tests use the same terms.

export const OUTCOME_LABEL: Record<Outcome, string> = {
  AUTHORIZED: "Authorized (automated match)",
  UNRECOGNIZED: "Unrecognized",
  INCONCLUSIVE: "Inconclusive",
  NOT_MATCHED: "Not matched",
  SYSTEM_ERROR: "System error",
};

export const OUTCOME_HELP: Record<Outcome, string> = {
  AUTHORIZED: "Looked like an enrolled, authorized person (similarity at or above the accept threshold)",
  UNRECOGNIZED: "Did not look like anyone enrolled (similarity at or below the reject threshold)",
  INCONCLUSIVE: "Between the thresholds, two people matched, or the face was too small to compare",
  NOT_MATCHED: "Recognition was off or unavailable when this event was analysed",
  SYSTEM_ERROR: "The face could not be compared (no usable embedding)",
};

export const OUTCOME_CLASS: Record<Outcome, string> = {
  AUTHORIZED: "status-ok",
  UNRECOGNIZED: "status-bad",
  INCONCLUSIVE: "status-warn",
  NOT_MATCHED: "status-neutral",
  SYSTEM_ERROR: "status-bad",
};

export const REASON_LABEL: Record<OutcomeReason, string> = {
  match: "Automated match: similar enough to an enrolled person (not a verified identity)",
  below_reject_threshold: "Not similar to anyone enrolled",
  gallery_empty: "Nobody enrolled to compare with",
  between_thresholds: "Between the thresholds: no decision",
  several_people_match: "Similar to more than one enrolled person: no decision",
  insufficient_quality: "Face too small to compare",
  recognition_error: "Recognition error: no usable embedding",
  recognition_off: "Recognition was off",
  recognition_unavailable: "Recognition unavailable (embedder not ready)",
  not_performed: "Recognition not performed",
};

export const STATUS_LABEL: Record<FaceEventStatus, string> = {
  faces_detected: "Faces detected",
  no_faces_detected: "No face detected",
  frame_unavailable: "No frame from Frigate",
  frame_rejected: "Frame rejected",
  inference_failed: "Detector failed",
  gave_up: "Not analysed (gave up)",
};

export function recognitionStateLabel(state: FaceEvent["recognition_state"]): string {
  switch (state) {
    case "done":
      return "Recognition ran";
    case "off":
      return "Recognition was off";
    case "unavailable":
      return "Recognition unavailable (embedder not ready)";
    default:
      return "Before recognition existed";
  }
}

export const formatSimilarity = (value: number | null | undefined) => (value == null ? "—" : value.toFixed(3));

export const pct = (value: number, total: number) => `${(value / total) * 100}%`;
