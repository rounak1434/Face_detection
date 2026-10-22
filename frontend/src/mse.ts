// Minimal client for go2rtc's MSE websocket protocol (relayed by the API):
//   1. client sends {"type":"mse","value":"<comma-separated codecs it supports>"}
//   2. server replies {"type":"mse","value":"video/mp4; codecs=\"...\""}
//   3. server sends fMP4 fragments as binary messages
// Protocol details must be confirmed against the running go2rtc version.

const CODEC_CANDIDATES = [
  "avc1.640029",
  "avc1.64002A",
  "avc1.640033",
  "hvc1.1.6.L153.B0",
  "mp4a.40.2",
  "mp4a.40.5",
  "flac",
  "opus",
];

// Keep live latency low: jump forward if playback falls this far behind.
const MAX_LAG_SECONDS = 2;

export function mseSupported(): boolean {
  return typeof window !== "undefined" && "MediaSource" in window;
}

export function supportedCodecs(isSupported: (mime: string) => boolean): string {
  return CODEC_CANDIDATES.filter((c) => isSupported(`video/mp4; codecs="${c}"`)).join(",");
}

export type LiveHandle = { close: () => void };

export function startMseLive(
  video: HTMLVideoElement,
  wsUrl: string,
  onError: (reason: string) => void,
): LiveHandle {
  const mediaSource = new MediaSource();
  const objectUrl = URL.createObjectURL(mediaSource);
  video.src = objectUrl;

  let ws: WebSocket | null = null;
  let buffer: SourceBuffer | null = null;
  const queue: ArrayBuffer[] = [];
  let closed = false;

  const pump = () => {
    if (!buffer || buffer.updating || queue.length === 0) return;
    try {
      buffer.appendBuffer(queue.shift()!);
    } catch (err) {
      fail(`append failed: ${(err as Error).name}`);
    }
  };

  const keepLive = () => {
    if (!buffer || buffer.buffered.length === 0) return;
    const end = buffer.buffered.end(buffer.buffered.length - 1);
    if (end - video.currentTime > MAX_LAG_SECONDS) video.currentTime = end - 0.3;
  };

  const fail = (reason: string) => {
    if (closed) return;
    close();
    onError(reason);
  };

  const close = () => {
    closed = true;
    ws?.close();
    URL.revokeObjectURL(objectUrl);
    video.removeAttribute("src");
    video.load();
  };

  mediaSource.addEventListener("sourceopen", () => {
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      const codecs = supportedCodecs((m) => MediaSource.isTypeSupported(m));
      ws!.send(JSON.stringify({ type: "mse", value: codecs }));
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "mse" && !buffer) {
          try {
            buffer = mediaSource.addSourceBuffer(msg.value);
            buffer.mode = "segments";
            buffer.addEventListener("updateend", () => {
              keepLive();
              pump();
            });
          } catch {
            fail(`unsupported stream format: ${msg.value}`);
          }
        } else if (msg.type === "error") {
          fail(String(msg.value));
        }
        return;
      }
      queue.push(ev.data as ArrayBuffer);
      pump();
    };
    ws.onerror = () => fail("live connection error");
    ws.onclose = () => fail("live connection closed");
  });

  video.play().catch(() => {
    // autoplay may be blocked until data arrives; the element is muted
  });

  return { close };
}
