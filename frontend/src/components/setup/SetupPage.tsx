import { useCallback, useEffect, useState } from "react";
import { api, Device, DeviceInput } from "../../api";
import { DeviceCard } from "./DeviceCard";
import { DeviceForm } from "./DeviceForm";
import { WebcamPreview } from "./WebcamPreview";

type Editing = { mode: "new" } | { mode: "edit"; device: Device } | null;

export function SetupPage() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing>(null);
  const [testing, setTesting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDevices(await api.devices());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const replace = (d: Device) => setDevices((list) => list.map((x) => (x.id === d.id ? d : x)));

  const save = async (input: DeviceInput) => {
    if (editing?.mode === "edit") replace(await api.updateDevice(editing.device.id, input));
    else {
      const created = await api.createDevice(input);
      setDevices((list) => [...list, created]);
    }
    setEditing(null);
  };

  const test = async (d: Device) => {
    setTesting(d.id);
    setError(null);
    try {
      replace(await api.testDevice(d.id));
    } catch (err) {
      setError(`${d.name}: ${(err as Error).message}`);
    } finally {
      setTesting(null);
    }
  };

  const remove = async (d: Device) => {
    if (!window.confirm(`Remove "${d.name}"? Its stored credentials are deleted.`)) return;
    try {
      await api.deleteDevice(d.id);
      setDevices((list) => list.filter((x) => x.id !== d.id));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const addSynthetic = async () => {
    try {
      const d = await api.addSynthetic();
      setDevices((list) => (list.some((x) => x.id === d.id) ? list : [...list, d]));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const synthetic = devices.filter((d) => d.kind === "synthetic");
  const real = devices.filter((d) => d.kind !== "synthetic");

  const card = (d: Device) => (
    <DeviceCard key={d.id} device={d} testing={testing === d.id} onTest={() => test(d)}
      onEdit={d.editable ? () => setEditing({ mode: "edit", device: d }) : undefined} onRemove={() => remove(d)} />
  );

  return (
    <div className="setup">
      <h1 className="page-title">Camera &amp; NVR Setup</h1>
      <p className="notice">
        Saving a device here stores and tests its connection. The engine does not yet stream saved devices
        automatically: the live view still uses the streams configured in <code>deploy/.env</code> (see SETUP.md).
        A device shows <strong>Connected</strong> only after a test in which the server actually decoded video
        from it.
      </p>
      {error && <p className="error" role="alert">{error}</p>}

      <WebcamPreview />

      <section className="card source source-synthetic" aria-labelledby="synthetic-title">
        <header className="source-head">
          <h2 id="synthetic-title">Synthetic RTSP test source</h2>
          <span className="source-tag tag-synthetic">Test pattern, not a camera</span>
        </header>
        <p className="muted">
          A generated test pattern served over RTSP by the stack&apos;s <code>test-stream</code> container.
          Use it to test the real backend and video pipeline without camera hardware. Its address is set on
          the server and cannot be changed here. It is only reachable when the Docker stack runs with the{" "}
          <code>test-stream</code> profile.
        </p>
        {synthetic.length === 0 ? (
          <div className="actions">
            <button onClick={addSynthetic}>Add synthetic test source</button>
          </div>
        ) : (
          synthetic.map(card)
        )}
      </section>

      <section className="card source source-real" aria-labelledby="real-title">
        <header className="source-head">
          <h2 id="real-title">IP cameras and NVRs</h2>
          <span className="source-tag tag-real">Real devices</span>
        </header>
        <p className="muted">
          Devices on your local network. Only private LAN addresses are accepted, and connection tests only
          contact devices saved here.
        </p>
        {real.length === 0 && !editing && <p className="muted">No devices configured yet.</p>}
        {real.map((d) =>
          editing?.mode === "edit" && editing.device.id === d.id ? (
            <DeviceForm key={d.id} initial={d} onSubmit={save} onCancel={() => setEditing(null)} />
          ) : (
            card(d)
          ),
        )}
        {editing?.mode === "new" ? (
          <DeviceForm onSubmit={save} onCancel={() => setEditing(null)} />
        ) : (
          <div className="actions">
            <button onClick={() => setEditing({ mode: "new" })} disabled={editing !== null}>
              Add camera or NVR
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
