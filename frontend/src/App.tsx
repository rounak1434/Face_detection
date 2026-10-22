import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Camera, Health } from "./api";
import { CameraTile } from "./components/CameraTile";
import { Captures } from "./components/Captures";
import { Login } from "./components/Login";
import { SetupPage } from "./components/setup/SetupPage";

const DEFAULT_APP_NAME = "AI Surveillance";

export function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined); // undefined = checking
  const [health, setHealth] = useState<Health | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [page, setPage] = useState<"live" | "setup">("live");

  const appName = health?.app ?? DEFAULT_APP_NAME;

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.me().then((me) => setUser(me.username)).catch(() => setUser(null));
  }, []);

  useEffect(() => {
    document.title = appName;
  }, [appName]);

  const loadCameras = useCallback(async () => {
    try {
      setCameras(await api.cameras());
      setCameraError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setUser(null);
      setCameraError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    if (user) loadCameras();
  }, [user, loadCameras]);

  if (user === undefined) return <p className="center muted">Loading…</p>;
  if (user === null) return <Login appName={appName} onSignedIn={setUser} />;

  const logout = async () => {
    await api.logout().catch(() => undefined);
    setUser(null);
  };

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-left">
          <h1>{appName}</h1>
          <nav className="tabs" aria-label="Sections">
            <button className={page === "live" ? "tab active" : "tab"} aria-current={page === "live" ? "page" : undefined}
              onClick={() => setPage("live")}>Live</button>
            <button className={page === "setup" ? "tab active" : "tab"} aria-current={page === "setup" ? "page" : undefined}
              onClick={() => setPage("setup")}>Camera &amp; NVR Setup</button>
          </nav>
        </div>
        <div className="topbar-right">
          <span className={`dot ${health?.frigate.reachable ? "ok" : "bad"}`} />
          <span className="muted">
            {health?.frigate.reachable ? `Engine ${health.frigate.version}` : "Engine unreachable"}
          </span>
          <span className="muted">{user}</span>
          <button className="secondary" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="content">
        {page === "setup" ? (
          <SetupPage />
        ) : (
          <>
            {cameraError && <p className="error">Cameras unavailable: {cameraError}</p>}
            {!cameraError && cameras.length === 0 && <p className="muted">No cameras configured.</p>}
            <section className="grid">
              {cameras.map((c) => (
                <CameraTile key={c.name} camera={c} onCaptured={() => setRefreshKey((k) => k + 1)} />
              ))}
            </section>
            <Captures refreshKey={refreshKey} />
          </>
        )}
      </main>
    </div>
  );
}
