import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { mseSupported, startMseLive } from "../mse";

type Mode = "connecting" | "live" | "fallback";

const FALLBACK_INTERVAL_MS = 1000;

// Live video via MSE; if that is unavailable, refresh a still frame each second.
export function LiveView({ camera }: { camera: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [mode, setMode] = useState<Mode>(mseSupported() ? "connecting" : "fallback");
  const [reason, setReason] = useState<string | null>(mseSupported() ? null : "browser lacks MSE");
  const [frameUrl, setFrameUrl] = useState(() => api.latestFrameUrl(camera));

  useEffect(() => {
    if (mode === "fallback" || !videoRef.current) return;
    const video = videoRef.current;
    const onPlaying = () => setMode("live");
    video.addEventListener("playing", onPlaying);
    const handle = startMseLive(video, api.liveSocketUrl(camera), (why) => {
      setReason(why);
      setMode("fallback");
    });
    return () => {
      video.removeEventListener("playing", onPlaying);
      handle.close();
    };
    // Reconnect only when the camera changes, not when the mode flips to "live".
  }, [camera]);

  useEffect(() => {
    if (mode !== "fallback") return;
    const timer = window.setInterval(() => setFrameUrl(api.latestFrameUrl(camera)), FALLBACK_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [mode, camera]);

  return (
    <div className="live">
      {mode === "fallback" ? (
        <img src={frameUrl} alt={`Latest frame from ${camera}`} />
      ) : (
        <video ref={videoRef} muted playsInline autoPlay aria-label={`Live view of ${camera}`} />
      )}
      <span className={`badge badge-${mode}`} title={reason ?? undefined}>
        {mode === "live" ? "Live" : mode === "connecting" ? "Connecting…" : "Still frames"}
      </span>
    </div>
  );
}
