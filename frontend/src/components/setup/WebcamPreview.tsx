import { useEffect, useRef, useState } from "react";

// Shows the laptop webcam using the browser's own camera access. The video
// never leaves the browser: nothing is sent to the server, the engine
// (Frigate) never sees it, and it does not test the RTSP pipeline.
export function WebcamPreview() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [error, setError] = useState<string | null>(null);

  const supported = typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

  const start = async () => {
    setError(null);
    try {
      const media = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      setStream(media);
    } catch (err) {
      const name = (err as DOMException).name;
      setError(
        name === "NotAllowedError"
          ? "Camera permission was denied in the browser."
          : name === "NotFoundError"
            ? "No webcam was found."
            : `Could not start the webcam (${name || "unknown error"}).`,
      );
    }
  };

  const stop = () => {
    stream?.getTracks().forEach((t) => t.stop());
    setStream(null);
  };

  useEffect(() => {
    if (videoRef.current) videoRef.current.srcObject = stream;
  }, [stream]);

  // Release the camera when leaving the page.
  useEffect(() => () => stream?.getTracks().forEach((t) => t.stop()), [stream]);

  return (
    <section className="card source source-webcam" aria-labelledby="webcam-title">
      <header className="source-head">
        <h2 id="webcam-title">Browser webcam</h2>
        <span className="source-tag tag-webcam">Local preview only</span>
      </header>
      <p className="muted">
        Shows this computer&apos;s webcam using your browser&apos;s camera permission, to check the layout
        and controls. The video stays in this browser: it is not sent to the server, not recorded, and{" "}
        <strong>it does not test the RTSP or engine pipeline</strong>.
      </p>
      {!supported && (
        <p className="error">This browser does not allow camera access here (it needs https or localhost).</p>
      )}
      <div className="live webcam-frame">
        {stream ? (
          <video ref={videoRef} autoPlay muted playsInline aria-label="Webcam preview" />
        ) : (
          <p className="center-text muted">Webcam is off</p>
        )}
      </div>
      <div className="actions">
        {stream ? (
          <button onClick={stop}>Stop webcam</button>
        ) : (
          <button onClick={start} disabled={!supported}>Start webcam preview</button>
        )}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
