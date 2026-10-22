import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Camera, Health } from "./api";
import { CameraTile } from "./components/CameraTile";
import { Captures } from "./components/Captures";
import { Login } from "./components/Login";

const DEFAULT_APP_NAME = "AI Surveillance";

export function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined); // undefined = checking
  const [health, setHealth] = useState<Health | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

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
        <h1>{appName}</h1>
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
        {cameraError && <p className="error">Cameras unavailable: {cameraError}</p>}
        {!cameraError && cameras.length === 0 && <p className="muted">No cameras configured.</p>}
        <section className="grid">
          {cameras.map((c) => (
            <CameraTile key={c.name} camera={c} onCaptured={() => setRefreshKey((k) => k + 1)} />
          ))}
        </section>
        <Captures refreshKey={refreshKey} />
      </main>
    </div>
  );
}
