const API_BASE = import.meta.env.VITE_CORTEX_API_BASE || "http://127.0.0.1:8000";
let bridgeConnection;

async function desktopBridge() {
  if (!window.qt?.webChannelTransport) return null;
  if (!bridgeConnection) {
    bridgeConnection = connectDesktopBridge().catch(error => { bridgeConnection = null; throw error; });
  }
  return bridgeConnection;
}

async function connectDesktopBridge() {
  if (!window.QWebChannel) {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("The desktop connection timed out.")), 8000);
      const script = document.createElement("script");
      script.src = "qrc:///qtwebchannel/qwebchannel.js";
      script.onload = () => { clearTimeout(timer); resolve(); };
      script.onerror = () => { clearTimeout(timer); reject(new Error("The desktop connection could not be loaded.")); };
      document.head.appendChild(script);
    });
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Cortex desktop did not connect.")), 8000);
    new window.QWebChannel(window.qt.webChannelTransport,
      channel => { clearTimeout(timer); resolve(channel.objects.bridge); });
  });
}

export function createControlClient(onVoiceState, onAudioLevel) {
  const pending = new Map();
  let counter = 0;
  let disposed = false;
  let native = null;
  const receive = raw => {
    let message;
    try { message = JSON.parse(raw); } catch { return; }
    const entry = pending.get(message.id);
    if (!entry) return;
    pending.delete(message.id);
    clearTimeout(entry.timer);
    message.ok ? entry.resolve(message.data) : entry.reject(new Error(message.error || "Cortex rejected this operation."));
  };
  const ready = desktopBridge().then(bridge => {
    if (disposed) return null;
    native = bridge;
    bridge?.controlResponse?.connect(receive);
    bridge?.coreStateChanged?.connect(onVoiceState);
    bridge?.audioLevelChanged?.connect(onAudioLevel);
    bridge?.uiReady?.();
    return bridge;
  });
  return {
    async request(method, path, body) {
      const bridge = await ready;
      if (disposed) throw new Error("The desktop connection was closed.");
      if (bridge?.controlRequest) {
        return new Promise((resolve, reject) => {
          const id = `ui-${++counter}`;
          const timer = setTimeout(() => {
            pending.delete(id);
            reject(new Error("Cortex did not confirm this operation. Refresh before retrying."));
          }, path === "/desktop/runtime" ? 45000 : 12000);
          pending.set(id, { resolve, reject, timer });
          bridge.controlRequest(JSON.stringify({ id, method, path, body }));
        });
      }
      const response = await fetch(API_BASE + path, {
        method, credentials: "include", headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(12000),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Cortex returned HTTP ${response.status}. Open the desktop app to use enrolled-device controls.`);
      return data;
    },
    dispose() {
      disposed = true;
      native?.controlResponse?.disconnect?.(receive);
      native?.coreStateChanged?.disconnect?.(onVoiceState);
      native?.audioLevelChanged?.disconnect?.(onAudioLevel);
      for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error("Connection closed.")); }
      pending.clear();
    },
  };
}

export function taskResult(task) {
  try { return (typeof task.context === "string" ? JSON.parse(task.context) : task.context)?.result || null; }
  catch { return null; }
}

export function humanStatus(value) {
  return String(value || "unknown").replaceAll("_", " ").toLowerCase();
}
