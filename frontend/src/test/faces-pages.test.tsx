import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FaceEvent, Person, RecognitionStatus } from "../api";
import { FaceEventsPage } from "../components/faces/FaceEventsPage";
import { PeoplePage } from "../components/faces/PeoplePage";

type Call = { method: string; path: string; body: unknown };
type Reply = { status: number; body?: unknown };

function mockFetch(handler: (method: string, path: string, body: unknown) => Reply) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body instanceof FormData ? init.body : init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, path: input, body });
      const r = handler(method, input, body);
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status, headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const detection = (id: string, outcome: string | null, person: { id: string; display_name: string | null } | null = null) => ({
  id, position: 0, box: { x: 100, y: 50, w: 80, h: 100 }, score: 0.93, keypoints5: [], quality: { face_px: 80 },
  crop_url: `/api/face-events/e/detections/${id}/crop`,
  recognition: outcome === null ? null : {
    outcome, person, similarity: 0.617, thresholds: { accept: 0.45, reject: 0.3 },
    reason: ({ AUTHORIZED: "match", UNRECOGNIZED: "below_reject_threshold", INCONCLUSIVE: "insufficient_quality" } as
      Record<string, string>)[outcome] ?? "not_performed",
  },
});

const ev = (id: string, detections: unknown[], over: Partial<FaceEvent> = {}): FaceEvent => ({
  id, frigate_event_id: `1.0-${id}`, camera: "entrance", started_at: "2026-10-30T10:00:00+00:00",
  ended_at: "2026-10-30T10:00:05+00:00", status: detections.length ? "faces_detected" : "no_faces_detected",
  face_count: detections.length, frame_source: "recording", frame: { width: 1920, height: 1080 },
  frame_url: detections.length ? `/api/face-events/${id}/frame` : null, detector: "yunet 2023mar", detail: null,
  recognition_state: "done", recognizer: "sface 2021dec", detections: detections as FaceEvent["detections"], ...over,
});

const asha: Person = {
  id: "p1", display_name: "Asha Verma", notes: null, consent: { date: "2026-10-01", note: "signed form #12" },
  active: true, face_count: 1, created_by: "admin", created_at: "2026-10-01T00:00:00+00:00",
  updated_at: "2026-10-01T00:00:00+00:00",
};

const recognitionOff: RecognitionStatus = {
  enabled: false, updated_at: null, updated_by: null,
  embedder: { ready: true, model: "sface 2021dec", status: "ready" },
  gallery: { people: 1, reference_faces: 1, pending: 0, unreadable: null },
  thresholds: { accept: 0.45, reject: 0.3, min_face_px: 40 },
};

describe("FaceEventsPage", () => {
  const events = [
    ev("e1", [detection("d1", "AUTHORIZED", { id: "p1", display_name: "Asha Verma" })]),
    ev("e2", [detection("d2", "UNRECOGNIZED"), { ...detection("d3", "INCONCLUSIVE"), position: 1 }]),
    ev("e3", [detection("d4", "AUTHORIZED", null)]),
    ev("e4", [], { recognition_state: "off" }),
    ev("e5", [detection("d5", null)], { recognition_state: null }),
  ];

  it("shows every outcome, the matched person and recognition states", async () => {
    mockFetch((_method, path) =>
      path.startsWith("/api/people") ? { status: 200, body: { items: [asha] } }
        : { status: 200, body: { items: events, next_cursor: null } });
    render(<FaceEventsPage />);
    await screen.findByText("Authorized: Asha Verma");
    const list = within(screen.getByRole("region", { name: "Face event list" }));
    expect(list.getByText("Unrecognized")).toBeInTheDocument();
    expect(list.getByText("Inconclusive")).toBeInTheDocument();
    expect(list.getByText("Authorized: deleted person")).toBeInTheDocument();
    expect(list.getByText("Recognition was off")).toBeInTheDocument();
    expect(list.getByText("No result")).toBeInTheDocument();
  });

  it("sends the filters and pages with the cursor", async () => {
    const calls = mockFetch((_method, path) => {
      if (path.startsWith("/api/people")) return { status: 200, body: { items: [asha] } };
      if (path.includes("cursor=c1")) return { status: 200, body: { items: [events[1]], next_cursor: null } };
      return { status: 200, body: { items: [events[0]], next_cursor: "c1" } };
    });
    render(<FaceEventsPage />);
    await screen.findByText("Authorized: Asha Verma");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await within(screen.getByRole("region", { name: "Face event list" })).findByText("Unrecognized")).toBeInTheDocument();
    expect(screen.getByText("Authorized: Asha Verma")).toBeInTheDocument(); // appended, not replaced

    const form = screen.getByRole("form", { name: "Filters" });
    fireEvent.change(within(form).getByLabelText("Outcome"), { target: { value: "AUTHORIZED" } });
    await screen.findByRole("option", { name: "Asha Verma" });
    fireEvent.change(within(form).getByLabelText("Person"), { target: { value: "p1" } });
    fireEvent.change(within(form).getByLabelText("Camera"), { target: { value: " entrance " } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(calls.some((c) => c.path.includes("outcome=AUTHORIZED"))).toBe(true));
    const last = calls.filter((c) => c.path.startsWith("/api/face-events")).at(-1)!.path;
    expect(last).toContain("person_id=p1");
    expect(last).toContain("camera=entrance");
    expect(last).not.toContain("cursor=");
  });

  it("distinguishes no events from no matches", async () => {
    mockFetch((_method, path) =>
      path.startsWith("/api/people") ? { status: 200, body: { items: [] } } : { status: 200, body: { items: [], next_cursor: null } });
    render(<FaceEventsPage />);
    expect(await screen.findByText("No face events yet.")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Filters" });
    fireEvent.change(within(form).getByLabelText("Status"), { target: { value: "gave_up" } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByText("No face events match these filters.")).toBeInTheDocument();
  });

  it("shows the frame with boxes and deletes only after confirmation", async () => {
    const calls = mockFetch((method, path) => {
      if (path.startsWith("/api/people")) return { status: 200, body: { items: [] } };
      if (method === "DELETE") return { status: 204 };
      return { status: 200, body: { items: [events[0]], next_cursor: null } };
    });
    render(<FaceEventsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Face event entrance/ }));
    const dialog = screen.getByRole("dialog", { name: "Face event" });
    expect(within(dialog).getByAltText("Frame from entrance")).toHaveAttribute("src", "/api/face-events/e1/frame");
    expect(within(dialog).getByLabelText("Face 1")).toHaveStyle({ left: `${(100 / 1920) * 100}%` });
    expect(within(dialog).getByText(/similarity 0.617/)).toBeInTheDocument();
    expect(within(dialog).getByText("Similar enough to an enrolled person")).toBeInTheDocument();
    expect(within(dialog).getByText(/no liveness check/)).toBeInTheDocument();

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete event" }));
    expect(confirm).toHaveBeenCalled();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);

    confirm.mockReturnValueOnce(true);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete event" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.find((c) => c.method === "DELETE")!.path).toBe("/api/face-events/e1");
    expect(screen.queryByRole("button", { name: /Face event entrance/ })).not.toBeInTheDocument();
  });

  it("says when a face was too small to compare", async () => {
    mockFetch((_method, path) =>
      path.startsWith("/api/people") ? { status: 200, body: { items: [] } }
        : { status: 200, body: { items: [ev("e9", [detection("d9", "INCONCLUSIVE")])], next_cursor: null } });
    render(<FaceEventsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Face event entrance/ }));
    const dialog = screen.getByRole("dialog", { name: "Face event" });
    expect(within(dialog).getByText("Face too small to compare")).toBeInTheDocument();
    expect(within(dialog).getByText("Inconclusive")).toHaveAttribute("title", "Face too small to compare");
  });

  it("reports an unavailable API", async () => {
    mockFetch((_method, path) =>
      path.startsWith("/api/people") ? { status: 200, body: { items: [] } } : { status: 500, body: { detail: "database locked" } });
    render(<FaceEventsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Face events unavailable: database locked");
  });
});

describe("PeoplePage", () => {
  const detail = { ...asha, faces: [{
    id: "f1", image: { width: 480, height: 600, media_type: "image/jpeg" }, image_url: "/api/people/p1/faces/f1/image",
    box: { x: 150, y: 100, w: 120, h: 150 }, score: 0.95, keypoints5: [], quality: {}, detector: "yunet 2023mar",
    embedded: false, embedder: null, created_by: "admin", created_at: "2026-10-01T00:00:00+00:00",
  }] };

  function routes(over: (m: string, p: string, b: unknown) => Reply | undefined = () => undefined) {
    return mockFetch((method, path, body) => {
      const r = over(method, path, body);
      if (r) return r;
      if (path === "/api/recognition") return { status: 200, body: recognitionOff };
      if (path.startsWith("/api/audit-log")) return { status: 200, body: { items: [], next_cursor: null } };
      if (path === "/api/people/p1") return { status: 200, body: detail };
      if (path === "/api/people") return { status: 200, body: { items: [asha] } };
      return { status: 404, body: { detail: "not mocked" } };
    });
  }

  it("shows the switch off with embedder and gallery state", async () => {
    routes();
    render(<PeoplePage />);
    expect(await screen.findByText("Ready (sface 2021dec)")).toBeInTheDocument();
    expect(screen.getByTestId("recognition-state")).toHaveTextContent("Off");
    expect(screen.getByText("1 active person, 1 reference image")).toBeInTheDocument();
    expect(screen.getByText(/no liveness check/)).toBeInTheDocument();
  });

  it("disables switching on while the embedder is not ready", async () => {
    routes((_m, p) => p === "/api/recognition"
      ? { status: 200, body: { ...recognitionOff, embedder: { ready: false, model: null, status: "missing" } } } : undefined);
    render(<PeoplePage />);
    expect(await screen.findByText("Not ready (missing)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch recognition on" })).toBeDisabled();
  });

  it("switches on only with the password, and shows wrong-password and refusal messages", async () => {
    let attempt = 0;
    let server = recognitionOff; // what the server would report after each change
    const calls = routes((m, p) => {
      if (p !== "/api/recognition") return undefined;
      if (m === "GET") return { status: 200, body: server };
      attempt += 1;
      if (attempt === 1) return { status: 403, body: { detail: "Password is incorrect" } };
      if (attempt === 2) return { status: 409, body: { error: "gallery_empty", detail: "x" } };
      server = { ...recognitionOff, enabled: true, updated_by: "admin", updated_at: "2026-10-30T10:00:00+00:00" };
      return { status: 200, body: server };
    });
    render(<PeoplePage />);
    const open = () => fireEvent.click(screen.getByRole("button", { name: "Switch recognition on" }));
    await screen.findByText("Ready (sface 2021dec)");
    open();
    let dialog = screen.getByRole("dialog", { name: "Switch recognition on" });
    expect(within(dialog).getByRole("button", { name: "Switch on" })).toBeDisabled(); // no password yet
    fireEvent.change(within(dialog).getByLabelText("Your password"), { target: { value: "wrong" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch on" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Password is incorrect.");

    fireEvent.change(within(dialog).getByLabelText("Your password"), { target: { value: "right-password" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch on" }));
    expect(await screen.findByText(/No active person has a usable reference image/)).toBeInTheDocument();

    open();
    dialog = screen.getByRole("dialog", { name: "Switch recognition on" });
    fireEvent.change(within(dialog).getByLabelText("Your password"), { target: { value: "right-password" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch on" }));
    await waitFor(() => expect(screen.getByTestId("recognition-state")).toHaveTextContent("On"));
    const puts = calls.filter((c) => c.method === "PUT");
    expect(puts.at(-1)!.body).toEqual({ enabled: true, password: "right-password" });
    expect(screen.getByRole("button", { name: "Switch recognition off" })).toBeInTheDocument();
  });

  it("defaults the consent date to the local date, not the UTC date", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    // 03:30 on 30 October in local time; in UTC+5:30 that is still 29 October.
    vi.setSystemTime(new Date(2026, 9, 30, 3, 30));
    try {
      routes();
      render(<PeoplePage />);
      fireEvent.click(await screen.findByRole("button", { name: "Add authorized person" }));
      const input = within(screen.getByRole("form", { name: "New authorized person" })).getByLabelText("Consent date");
      expect(input).toHaveValue("2026-10-30");
      expect(input).toHaveAttribute("max", "2026-10-30");
    } finally {
      vi.useRealTimers();
    }
  });

  it("creates a person with the consent record", async () => {
    const calls = routes((m, p, b) =>
      m === "POST" && p === "/api/people" ? { status: 201, body: { ...asha, ...(b as object), id: "p1", faces: [] } } : undefined);
    render(<PeoplePage />);
    fireEvent.click(await screen.findByRole("button", { name: "Add authorized person" }));
    const form = screen.getByRole("form", { name: "New authorized person" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Asha Verma" } });
    fireEvent.change(within(form).getByLabelText("Consent date"), { target: { value: "2026-10-01" } });
    fireEvent.change(within(form).getByLabelText(/Consent evidence/), { target: { value: "signed form #12" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add person" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      display_name: "Asha Verma", consent_date: "2026-10-01", consent_note: "signed form #12", notes: null });
  });

  it("enrolls an upload and shows each rejection reason", async () => {
    let tries = 0;
    const calls = routes((m, p) => {
      if (m !== "POST" || p !== "/api/people/p1/faces") return undefined;
      tries += 1;
      return tries === 1
        ? { status: 422, body: { error: "low_quality", detail: "x", reasons: ["the face is 60 px; at least 80 px is needed",
          "the face touches the image edge"] } }
        : { status: 201, body: detail.faces[0] };
    });
    render(<PeoplePage />);
    fireEvent.click(await screen.findByRole("button", { name: /Asha Verma/ }));
    const card = await screen.findByRole("article", { name: "Asha Verma" });
    expect(within(card).getByAltText("Reference image of Asha Verma")).toHaveAttribute("src", "/api/people/p1/faces/f1/image");
    expect(within(card).getByText("Not yet embedded")).toBeInTheDocument();
    const input = within(card).getByLabelText("Reference image file");
    const file = new File(["jpeg"], "a.jpg", { type: "image/jpeg" });
    fireEvent.change(input, { target: { files: [file] } });
    const alert = await within(card).findByRole("alert");
    expect(alert).toHaveTextContent("The face is not usable as a reference.");
    expect(alert).toHaveTextContent("the face touches the image edge");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(within(card).queryByRole("alert")).not.toBeInTheDocument());
    expect(calls.filter((c) => c.path === "/api/people/p1/faces")[0].body).toBeInstanceOf(FormData);

    fireEvent.change(input, { target: { files: [new File(["gif"], "a.gif", { type: "image/gif" })] } });
    expect(await within(card).findByRole("alert")).toHaveTextContent("Choose a JPEG, PNG or WebP image.");
    expect(calls.filter((c) => c.path === "/api/people/p1/faces")).toHaveLength(2);
  });

  it("removes a reference image only after confirmation", async () => {
    const calls = routes((m) => (m === "DELETE" ? { status: 204 } : undefined));
    render(<PeoplePage />);
    fireEvent.click(await screen.findByRole("button", { name: /Asha Verma/ }));
    const card = await screen.findByRole("article", { name: "Asha Verma" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    fireEvent.click(within(card).getByRole("button", { name: /Remove reference image/ }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(within(card).getByRole("button", { name: /Remove reference image/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(calls.find((c) => c.method === "DELETE")!.path).toBe("/api/people/p1/faces/f1");
  });

  it("deletes a person only with the password", async () => {
    let gone = false;
    const calls = routes((m, p) => {
      if (m === "DELETE" && p === "/api/people/p1") {
        gone = true;
        return { status: 204 };
      }
      if (p === "/api/people") return { status: 200, body: { items: gone ? [] : [asha] } };
      return undefined;
    });
    render(<PeoplePage />);
    fireEvent.click(await screen.findByRole("button", { name: /Asha Verma/ }));
    const card = await screen.findByRole("article", { name: "Asha Verma" });
    fireEvent.click(within(card).getByRole("button", { name: "Delete person" }));
    const dialog = screen.getByRole("dialog", { name: "Delete Asha Verma" });
    expect(within(dialog).getByText(/cannot be undone/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);

    fireEvent.click(within(card).getByRole("button", { name: "Delete person" }));
    const again = screen.getByRole("dialog", { name: "Delete Asha Verma" });
    fireEvent.change(within(again).getByLabelText("Your password"), { target: { value: "right-password" } });
    fireEvent.click(within(again).getByRole("button", { name: "Delete permanently" }));
    expect(await screen.findByText("No authorized people yet.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "DELETE")!.body).toEqual({ password: "right-password" });
  });
});
