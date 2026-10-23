import { useCallback, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api, Camera, Health } from "./api";
import { AnalysisPage } from "./components/analysis/AnalysisPage";
import { CameraTile } from "./components/CameraTile";
import { Captures } from "./components/Captures";
import { Login } from "./components/Login";
import { SetupPage } from "./components/setup/SetupPage";

const DEFAULT_APP_NAME = "AI Surveillance";

const tab = ({ isActive }: { isActive: boolean }) => (isActive ? "tab active" : "tab");

function LivePage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadCameras = useCallback(async () => {
    try {
      setCameras(await api.cameras());
      setCameraError(null);
    } catch (err) {
      setCameraError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    loadCameras();
  }, [loadCameras]);

  return (
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
  );
}

export function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined); // undefined = checking
  const [health, setHealth] = useState<Health | null>(null);

  const appName = health?.app ?? DEFAULT_APP_NAME;

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.me().then((me) => setUser(me.username)).catch(() => setUser(null));
  }, []);

  useEffect(() => {
    document.title = appName;
  }, [appName]);

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
            <NavLink to="/live" className={tab}>Live</NavLink>
            <NavLink to="/analysis" className={tab}>AI analysis</NavLink>
            <NavLink to="/setup" className={tab}>Camera &amp; NVR Setup</NavLink>
          </nav>
        </div>
        <div className="topbar-right">
          <span className={`dot ${health?.frigate.reachable ? "ok" : "bad"}`} />
          <span className="muted">
            {health?.frigate.reachable ? `Engine ${health.frigate.version}` : "Engine unreachable"}
          </span>
          <span className={`dot ${health?.inference?.reachable ? "ok" : "bad"}`} />
          <span className="muted">{health?.inference?.reachable ? "AI ready" : "AI unreachable"}</span>
          <span className="muted">{user}</span>
          <button className="secondary" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="content">
        <Routes>
          <Route path="/live" element={<LivePage />} />
          <Route path="/analysis" element={<AnalysisPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route path="*" element={<Navigate to="/live" replace />} />
        </Routes>
      </main>
    </div>
  );
}
