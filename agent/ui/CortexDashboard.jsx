import IntegrationStatus from "./IntegrationStatus.jsx";
import { useEffect, useRef, useState, useCallback } from "react";
import { CortexOrb } from "./CortexOrb.jsx";
import { createControlClient, humanStatus } from "./control-client.js";
import { isTerminalStatus, isActiveTask, pollTaskResults } from "./task-sync.js";

function Icon({ name, size = 20 }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true };
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    device: <><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></>,
    monitor: <><rect x="2.5" y="4" width="19" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></>,
    laptop: <><rect x="5" y="4" width="14" height="11" rx="1.5"/><path d="M2.5 19h19l-2.5-4H5z"/></>,
    phone: <><rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/></>,
    bolt: <path d="m13 2-9 12h7l-1 8 9-12h-7z"/>,
    brain: <><path d="M9.5 4.5A3 3 0 0 0 4 6a3 3 0 0 0 .5 5.5A3.5 3.5 0 0 0 8 17h1.5V4.5Z"/><path d="M14.5 4.5A3 3 0 0 1 20 6a3 3 0 0 1-.5 5.5A3.5 3.5 0 0 1 16 17h-1.5V4.5ZM9.5 9H7m7.5 3H17M12 3v18"/></>,
    pulse: <path d="M3 12h4l2-7 4 14 2-7h6"/>,
    shield: <path d="M12 3 4.5 6v5.5c0 4.6 3.2 7.6 7.5 9.5 4.3-1.9 7.5-4.9 7.5-9.5V6L12 3Z"/>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
    mic: <><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/></>,
    send: <path d="m4 4 17 8-17 8 3-8-3-8Zm3 8h14"/>,
    command: <><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M8 9h8M8 13h5"/></>,
    chevron: <path d="m9 18 6-6-6-6"/>,
    check: <path d="m5 12 4 4L19 6"/>,
    bell: <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></>,
    alert: <><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></>,
  };
  return <svg {...common}>{paths[name] || paths.pulse}</svg>;
}



const NAV = [["Overview", "grid"], ["Devices", "device"], ["Automations", "bolt"],
  ["Memory", "brain"], ["Activity", "pulse"], ["Security", "shield"], ["Settings", "settings"]];
const terminal = isTerminalStatus;
const date = value => value ? new Date(value).toLocaleString() : "Not reported";
function Empty({ children }) { return <p className="empty-state">{children}</p>; }
function Panel({ title, detail, children }) { return <section className="functional-panel"><div className="section-heading"><div><h3>{title}</h3>{detail && <p>{detail}</p>}</div></div>{children}</section>; }
function Badge({ value }) { return <span className={`status-badge status-${humanStatus(value).split(" ")[0]}`}>{humanStatus(value)}</span>; }

export default function CortexDashboard() {
  const [page, setPage] = useState("Overview");
  const [snapshot, setSnapshot] = useState({ devices: [], tasks: [], approvals: [], transfers: [], activities: [], system_health: {}, safety: {} });
  const [runtime, setRuntime] = useState({});
  const [connected, setConnected] = useState(false);
  const [syncedAt, setSyncedAt] = useState(null);
  const [latency, setLatency] = useState(null);
  const [selected, setSelected] = useState("");
  const [voiceState, setVoiceState] = useState("idle");
  const [audioLevel, setAudioLevel] = useState(0);
  const [command, setCommand] = useState("");
  const [chat, setChat] = useState([]);
  const [memories, setMemories] = useState([]);
  const [scheduled, setScheduled] = useState([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [memory, setMemory] = useState({ category: "preference", key: "", value: "" });
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [clock, setClock] = useState(new Date());
  const [reduceMotion, setReduceMotion] = useState(() => localStorage.getItem("cortex-reduce-motion") === "true");
  const client = useRef(null);
  const refreshing = useRef(false);
  const input = useRef(null);
  const chatRef = useRef([]);
  const pageRef = useRef(page);
  useEffect(() => { chatRef.current = chat; }, [chat]);
  useEffect(() => { pageRef.current = page; setQuery(""); setFilter("all"); setError(""); }, [page]);
  const go = value => { setPage(value); setNotice(""); };
  const request = useCallback((method, path, body) => client.current.request(method, path, body), []);

  const refresh = useCallback(async () => {
    if (!client.current || refreshing.current) return;
    const activeClient = client.current;
    let receivedState = false;
    refreshing.current = true;
    const started = performance.now();
    try {
      // Local runtime controls must stay available when the hub is stopped.
      const desktop = await request("GET", "/desktop/state").catch(() => null);
      if (client.current !== activeClient) return;
      if (desktop) { setRuntime(desktop); setVoiceState(desktop.voice || "idle"); }
      const data = await request("GET", "/api/control/state");
      if (client.current !== activeClient) return;
      receivedState = true;
      setConnectionError("");
      setSnapshot({ devices: [], tasks: [], approvals: [], transfers: [], activities: [], safety: {}, system_health: {}, ...data });
      setConnected(true); setSyncedAt(new Date()); setLatency(Math.round(performance.now() - started));
      setSelected(previous => data.devices?.some(d => d.device_id === previous) ? previous : (data.devices?.[0]?.device_id || ""));
      if (pageRef.current === "Memory") {
        const rows = await request("GET", "/memory/"); setMemories(rows.memory || []);
      }
      if (pageRef.current === "Automations") {
        const rows = await request("GET", "/tasks/"); setScheduled(rows.tasks || []);
      }
    } catch (problem) {
      if (client.current !== activeClient) return;
      if (receivedState) setError(problem.message);
      else { setConnectionError(problem.message || "The hub is unavailable."); setConnected(false); }
    } finally {
      if (client.current === activeClient) {
        try {
          // A failed snapshot must not suppress independent final-answer delivery.
          const updates = await pollTaskResults(chatRef.current, request);
          if (client.current === activeClient && updates.length) {
            setChat(previous => previous.map(message => {
              const update = updates.find(item => item.id === message.id);
              return update ? { ...message, ...update } : message;
            }));
          }
        } finally { if (client.current === activeClient) refreshing.current = false; }
      }
    }
  }, [request]);
  const refreshRef = useRef(refresh);
  useEffect(() => { refreshRef.current = refresh; }, [refresh]);
  useEffect(() => {
    const api = createControlClient(value => setVoiceState(String(value || "idle").toLowerCase()), value => setAudioLevel(Number(value) || 0));
    client.current = api;
    refreshRef.current();
    const poll = setInterval(() => refreshRef.current(), 3000);
    const tick = setInterval(() => setClock(new Date()), 1000);
    return () => { clearInterval(poll); clearInterval(tick); api.dispose(); if (client.current === api) { client.current = null; refreshing.current = false; } };
  }, []);
  useEffect(() => { refreshRef.current(); }, [page]);

  async function operate(key, method, path, body, success) {
    if (busy) return;
    setBusy(key); setError(""); setNotice("");
    try { const result = await request(method, path, body); if (success) setNotice(success); await refreshRef.current(); return result; }
    catch (problem) { setError(problem.message); return null; }
    finally { setBusy(""); }
  }
  async function submit(event) {
    event.preventDefault();
    const text = command.trim();
    if (!text || busy) return;
    if (!runtime.device_id) { setError("Open Cortex desktop with an enrolled device to send commands."); return; }
    setBusy("command"); setError("");
    try {
      const result = await request("POST", "/api/control/message", { text, request_id: crypto.randomUUID(), device_id: runtime.device_id, target_device: selected || undefined });
      setChat(previous => [...previous.slice(-29), { id: crypto.randomUUID(), text, response: result.response_text,
        status: result.status, task_id: result.task_id || result.data?.task_id, target: selected, source: runtime.device_id }]);
      setCommand(""); await refreshRef.current();
    } catch (problem) { setError(problem.message); }
    finally { setBusy(""); }
  }
  async function saveMemory(event) {
    event.preventDefault();
    if (!memory.key.trim() || !memory.value.trim()) return;
    const result = await operate("memory", editing ? "PUT" : "POST", editing ? `/memory/${editing}` : "/memory/",
      editing ? { value: memory.value } : memory, "Memory saved to Cortex.");
    if (result) { setEditing(null); setMemory({ category: "preference", key: "", value: "" }); }
  }
  const devices = snapshot.devices;
  const chosen = devices.find(d => d.device_id === selected);
  const online = connected ? devices.filter(d => d.online).length : 0;
  const activeTasks = snapshot.tasks.filter(task => isActiveTask(task, snapshot.tasks));
  const status = !connected ? "idle" : busy === "command" || activeTasks.length ? "working" :
    voiceState === "thinking" ? "working" : ["speaking", "listening"].includes(voiceState) ? voiceState : "idle";
  const matches = item => JSON.stringify(item).toLowerCase().includes(query.toLowerCase());
  const activity = snapshot.activities.filter(a => matches(a) && (filter === "all" || a.category === filter));
  const providers = snapshot.system_health.providers || [];
  const modelStatus = snapshot.system_health.model_status || {};
  const changeSafety = () => operate("autonomy", "POST", "/api/control/autonomy", { enabled: !snapshot.safety.autonomy_enabled },
    snapshot.safety.autonomy_enabled ? "Actions disabled. Active work was stopped." : "Actions enabled.");
  const stopTask = id => operate(`stop-${id}`, "POST", `/api/control/tasks/${id}/stop`, {}, "Task cancellation requested.");
  const voice = action => operate("voice", "POST", "/desktop/voice", { action }, "Voice control sent to the device agent.");
  const search = <div className="page-tools"><label className="search-field"><span>Search {page.toLowerCase()}</span><input aria-label={`Search ${page.toLowerCase()}`} value={query} onChange={e => setQuery(e.target.value)} placeholder="Filter this page…"/></label><button className="control-button" onClick={() => refreshRef.current()} disabled={!!busy}>Refresh</button></div>;
  const approvals = <>{snapshot.approvals.length ? snapshot.approvals.map(a => <div className="approval-banner" key={a.command_id}>
    <div className="approval-info"><span className="approval-badge">CONFIRMATION REQUIRED · TIER {a.tier || "—"}</span><strong>{a.action_summary || a.command}</strong>
      <small>Target: {a.target_device} · {a.privacy_risk || a.summary || "Review before allowing this action."}</small></div>
    <div className="approval-actions"><button className="btn-approve" disabled={!connected || !!busy} onClick={() => operate(a.command_id, "POST", `/api/control/approvals/${a.command_id}/resolve`, { decision: "approve" }, "Approval recorded by the hub.")}>Approve</button>
      <button className="btn-deny" disabled={!connected || !!busy} onClick={() => operate(a.command_id, "POST", `/api/control/approvals/${a.command_id}/resolve`, { decision: "deny" }, "Denial recorded by the hub.")}>Deny</button></div></div>) : <Empty>No pending approvals.</Empty>}</>;
  const taskList = <>{snapshot.tasks.length ? snapshot.tasks.filter(matches).map(t => <div className="data-row" key={t.id}><div><strong>{t.title || t.id}</strong><small>{t.executor || "Executor not reported"} · {t.waiting_reason || date(t.updated_at)}</small></div><Badge value={t.status}/>{!terminal(t.status) && <button className="control-button danger" disabled={!connected || !!busy} onClick={() => stopTask(t.id)}>Cancel task</button>}</div>) : <Empty>No tasks have been reported by the hub.</Empty>}</>;

  return <div className={`cortex-app ${reduceMotion ? "reduce-motion" : ""}`}>
    <aside className="sidebar"><div className="brand"><div className="brand-mark"><span/><span/><span/></div><div><strong>CORTEX</strong><small>COMMAND CENTER</small></div></div>
      <nav className="primary-nav" aria-label="Primary navigation"><p className="nav-label">WORKSPACE</p>{NAV.map(([label, icon]) => <button title={label} key={label} className={page === label ? "active" : ""} aria-current={page === label ? "page" : undefined} onClick={() => go(label)}><Icon name={icon}/><span>{label}</span>{label === "Security" && snapshot.approvals.length > 0 && <em>{snapshot.approvals.length}</em>}</button>)}</nav>
      <button className="sidebar-foot" onClick={() => go("Settings")}><div className="mini-core"><div/><span/></div><div><strong>{connected ? "Hub connected" : "Hub disconnected"}</strong><small>{syncedAt ? `Synced ${syncedAt.toLocaleTimeString()}` : "Waiting for a real connection"}</small></div></button></aside>
    <main className="main-panel"><header className="topbar"><div><p className="eyebrow">{page} / Cortex workspace</p><h1>{page === "Overview" ? "Your command center." : page}</h1></div><div className="top-actions"><span className={`connection-pill ${connected ? "online" : "offline"}`}>{connected ? "CONNECTED" : "OFFLINE"}</span><button className="icon-button" aria-label="Notifications and approvals" onClick={() => go("Security")}><Icon name={snapshot.approvals.length ? "alert" : "bell"}/></button><div className="time"><strong>{clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</strong><span>LOCAL TIME</span></div></div></header>
      <div className="workspace">
        {(error || connectionError) && <div className="feedback error" role="alert"><span>{error || connectionError}{!connected && syncedAt ? " Displaying the last received snapshot." : ""}</span><button onClick={() => { setError(""); refreshRef.current(); }}>Retry</button></div>}
        {notice && <div className="feedback" role="status">{notice}<button aria-label="Dismiss notification" onClick={() => setNotice("")}>×</button></div>}
        {page === "Overview" && <>
          <div className="orb-stage"><div className="stage-grid"/><div className="orb-meta"><span>CORTEX CORE</span><span>{connected ? "HUB CONNECTED" : "AWAITING CONNECTION"}</span></div>
            <CortexOrb status={reduceMotion ? "idle" : status} emotion="neutral" audioLevel={reduceMotion ? 0 : audioLevel} eye onPoke={() => voice("push_to_talk")}/>
            <div className="orb-copy"><p className="live-state"><span/>{connected ? humanStatus(voiceState).toUpperCase() : "OFFLINE"}</p><h2>{!connected ? (syncedAt ? "Dashboard status unavailable." : "Connect to get started.") : status === "listening" ? "I'm listening." : status === "working" ? "Work in progress." : status === "speaking" ? "Speaking." : "Ready when you are."}</h2><p>{connected ? `${chosen?.display_name || selected || "Choose a device"} · ${activeTasks.length} active tasks` : syncedAt ? "The last snapshot is stale. Chat task results are checked separately." : "Start Cortex from Settings. No commands are simulated."}</p></div></div>
          <form className="command-bar" onSubmit={submit}><div className="command-icon"><Icon name="command"/></div><label><span>DIRECT COMMAND · {chosen?.display_name || selected || "NO DEVICE SELECTED"}</span><input ref={input} value={command} onChange={e => setCommand(e.target.value)} placeholder="Ask Cortex to do something…" aria-label="Command"/></label><button type="button" className={`mic ${voiceState === "listening" ? "active" : ""}`} disabled={!runtime.ipc_connected || !!busy} onClick={() => voice("push_to_talk")} aria-label="Start listening"><Icon name="mic"/></button><button className="send" disabled={!connected || !!busy || !command.trim()} aria-label="Send command"><Icon name="send"/></button></form>
          <div className="metrics-row">{[[String(online), "ONLINE DEVICES", "Devices"], [latency === null ? "—" : `${latency}ms`, "HUB ROUND TRIP", "Settings"], [String(activeTasks.length), "ACTIVE TASKS", "Automations"], [String(snapshot.approvals.length), "PENDING APPROVALS", "Security"]].map(([value, label, target]) => <button className="metric" key={label} onClick={() => go(target)}><span className="metric-value">{value}</span><span className="metric-label">{label}</span><Icon name="chevron" size={11}/></button>)}</div>
          <Panel title="Conversation" detail="Actual responses and task outcomes from Cortex.">{chat.length ? <div className="conversation" aria-live="polite">{chat.map(m => <article className="chat-turn" key={m.id}><div><strong>You</strong><span>{m.target || runtime.device_id}</span></div><p>{m.text}</p><div><strong>Cortex</strong><Badge value={m.status}/></div><p>{m.response || "No response text was returned."}</p>{m.sync_error && <p className="inline-error" role="alert">{m.sync_error}</p>}{m.task_id && <small>Task {m.task_id}</small>}<button className="text-button" onClick={() => { setCommand(m.text); input.current?.focus(); }}>Use again</button></article>)}</div> : <Empty>Your commands and their results will appear here.</Empty>}</Panel>
          <Panel title="Tasks">{taskList}</Panel>
          {snapshot.approvals.length > 0 && <Panel title="Pending approvals">{approvals}</Panel>}
        </>}
        {page === "Devices" && <>{search}<Panel title="Enrolled devices" detail="Live heartbeat, role and advertised capabilities.">{devices.filter(matches).map(d => <article className={`device-card ${selected === d.device_id ? "selected" : ""}`} key={d.device_id}><div className="data-row"><div><strong>{d.display_name || d.device_id}</strong><small>{d.platform || "Platform not reported"} · {d.role || "Role not reported"}</small></div><Badge value={!connected ? "stale" : d.online ? "online" : "offline"}/><button className="control-button" onClick={() => setSelected(d.device_id)}>{selected === d.device_id ? "Selected" : "Select device"}</button></div><dl className="detail-grid"><dt>Device ID</dt><dd>{d.device_id}</dd><dt>Last heartbeat</dt><dd>{date(d.last_heartbeat || d.last_seen)}</dd><dt>Trust</dt><dd>{d.trust_level || "Not reported"}</dd><dt>Capabilities</dt><dd>{d.capabilities?.length || 0}</dd></dl><details><summary>Show supported capabilities</summary><div className="capability-tags">{(d.capabilities || []).map(c => <span key={c}>{c}</span>)}</div></details><button className="text-button" onClick={() => { setSelected(d.device_id); go("Overview"); setTimeout(() => input.current?.focus(), 0); }}>Write a command for this device</button></article>)}{!devices.length && <Empty>No devices have been reported. Start or reconnect your enrolled device agent.</Empty>}</Panel>
          <Panel title="File transfers">{snapshot.transfers.length ? snapshot.transfers.map(t => <div className="data-row" key={t.transfer_id || t.id}><div><strong>{t.filename}</strong><small>{t.source || t.source_device || t.source_device_id} → {t.destination || t.destination_device || t.destination_device_id}</small></div><Badge value={t.state}/>{!terminal(t.state) && <button className="control-button danger" disabled={!!busy || !connected} onClick={() => operate(t.transfer_id, "POST", `/api/control/transfers/${t.transfer_id || t.id}/cancel`, {}, "Transfer cancellation recorded.")}>Cancel transfer</button>}</div>) : <Empty>No file transfers.</Empty>}</Panel></>}
        {page === "Automations" && <>{search}<Panel title="Active and recent tasks">{taskList}</Panel><Panel title="Scheduled automations" detail="Pause or resume existing one-time and background schedules. Recurring cron execution is not implemented.">{scheduled.filter(t => !["agent_turn", "request", "orchestration"].includes(t.type)).filter(matches).map(t => <div className="data-row" key={t.id}><div><strong>{t.title}</strong><small>{t.assigned_device || "No target"} · {t.type} · {date(t.trigger_time)}</small>{t.last_error && <small className="inline-error">{t.last_error}</small>}</div><Badge value={t.status}/>{["pending", "paused"].includes(t.status) && t.type !== "recurring" && <button className="control-button" disabled={!connected || !!busy} onClick={() => operate(t.id, "POST", `/tasks/${t.id}/${t.status === "paused" ? "resume" : "pause"}`, {}, t.status === "paused" ? "Schedule resumed." : "Schedule paused.")}>{t.status === "paused" ? "Resume schedule" : "Pause schedule"}</button>}</div>)}{!scheduled.some(t => !["agent_turn", "request", "orchestration"].includes(t.type)) && <Empty>No scheduled automations exist yet. Use a direct command to start a task.</Empty>}</Panel><button className="control-button" onClick={() => { go("Overview"); setTimeout(() => input.current?.focus(), 0); }}>Write a task request</button></>}
        {page === "Memory" && <>{search}<Panel title={editing ? "Edit memory" : "Add memory"} detail="Stored in Cortex's memory database. No model request is required."><form className="memory-form" onSubmit={saveMemory}><label>Category<select value={memory.category} disabled={!!editing} onChange={e => setMemory({ ...memory, category: e.target.value })}><option value="preference">Preference</option><option value="note">Note</option><option value="context">Context</option></select></label><label>Name<input required maxLength={160} value={memory.key} disabled={!!editing} onChange={e => setMemory({ ...memory, key: e.target.value })}/></label><label className="full-field">Value<textarea required rows={3} maxLength={5000} value={memory.value} onChange={e => setMemory({ ...memory, value: e.target.value })}/></label><div className="button-row"><button className="control-button primary" disabled={!connected || !!busy}>{busy === "memory" ? "Saving…" : "Save memory"}</button>{editing && <button type="button" className="control-button" onClick={() => { setEditing(null); setMemory({ category: "preference", key: "", value: "" }); }}>Cancel edit</button>}</div></form></Panel><Panel title="Saved memory">{memories.filter(matches).map(m => <div className="data-row memory-row" key={m.id}><div><strong>{m.key}</strong><small>{m.category}</small><p>{m.value}</p></div><button className="control-button" onClick={() => { setEditing(m.id); setMemory({ category: m.category, key: m.key, value: m.value }); }}>Edit memory</button></div>)}{!memories.length && <Empty>No saved memory items.</Empty>}</Panel></>}
        {page === "Activity" && <>{search}<label className="category-filter">Category<select value={filter} onChange={e => setFilter(e.target.value)}><option value="all">All events</option>{[...new Set(snapshot.activities.map(a => a.category).filter(Boolean))].map(category => <option key={category}>{category}</option>)}</select></label><Panel title="Operational activity" detail="Real hub events. No fabricated synchronization or security events.">{activity.map((a, i) => <div className="data-row" key={`${a.timestamp}-${i}`}><div><strong>{a.label}</strong><small>{date(a.timestamp)} · {a.category} · {a.device_id || "Hub"}</small></div><Badge value={a.status}/></div>)}{!activity.length && <Empty>No matching events.</Empty>}</Panel></>}
        {page === "Security" && <><Panel title="Action controls" detail="Disabling actions stops active agent turns and pauses goals."><div className="data-row"><div><strong>{snapshot.safety.autonomy_enabled ? "Actions enabled" : "Actions disabled"}</strong><small>Existing permission and privacy checks still apply.</small></div><button className={`control-button ${snapshot.safety.autonomy_enabled ? "danger" : "primary"}`} disabled={!connected || !!busy} onClick={changeSafety}>{snapshot.safety.autonomy_enabled ? "Disable actions" : "Enable actions"}</button></div></Panel><Panel title="Pending approvals">{approvals}</Panel><Panel title="Provider availability" detail="Configuration does not prove a service has remaining quota.">{modelStatus.last_failure && <p className="inline-error">Last model failure: {humanStatus(modelStatus.last_failure)}</p>}{providers.map((p, i) => <div className="data-row" key={i}><div><strong>{p.name || p.provider || p.purpose || "Provider"}</strong><small>{p.purpose || "Service"}</small></div><Badge value={p.configured ? "configured" : "unconfigured"}/></div>)}{!providers.length && <Empty>Provider state has not been received.</Empty>}</Panel></>}
        {page === "Settings" && <><Panel title="Integrations" detail="Account authorization and service health from Cortex. API access is separate from AI model availability."><IntegrationStatus integrations={snapshot.system_health.integrations || []} connected={connected} busy={busy} setDefault={(provider_id, account_id) => operate("integration-default", "POST", "/integrations/default-account", { provider_id, account_id }, "Default integration account saved.")}/></Panel><Panel title="Runtime" detail="Manage processes started by this window. Processes launched in other terminals stay under their owner's control."><dl className="detail-grid"><dt>Hub</dt><dd>{runtime.core_url || "Desktop connection unavailable"}</dd><dt>Source device</dt><dd>{runtime.device_id || "Not reported"}</dd><dt>Agent</dt><dd>{runtime.agent || "Not reported"}</dd><dt>Voice</dt><dd>{humanStatus(voiceState)}</dd><dt>Model</dt><dd>{runtime.model || modelStatus.model || "Not reported"}</dd><dt>Provider</dt><dd>{runtime.provider || modelStatus.provider || "Not reported"}</dd></dl><div className="button-row">{[["start", "Start Cortex", true], ["stop", "Stop managed runtime", runtime.managed_agent || runtime.managed_hub], ["restart_agent", "Restart managed agent", runtime.managed_agent], ["restart_hub", "Restart managed hub", runtime.managed_hub]].map(([action, label, available]) => <button key={action} className="control-button" disabled={!runtime.device_id || !!busy || !available} onClick={() => operate(action, "POST", "/desktop/runtime", { action }, "Runtime operation finished.")}>{busy === action ? "Working…" : label}</button>)}</div></Panel><Panel title="Voice"><p>Controls the connected device agent, without changing operating-system microphone permissions.</p><div className="button-row"><button className="control-button" disabled={!runtime.ipc_connected || !!busy} onClick={() => voice("push_to_talk")}>Start listening</button><button className="control-button" disabled={!runtime.ipc_connected || !!busy} onClick={() => voice(voiceState === "muted" ? "unmute" : "mute")}>{voiceState === "muted" ? "Unmute voice" : "Mute voice"}</button></div></Panel><Panel title="Display preferences"><label className="toggle-field"><input type="checkbox" checked={reduceMotion} onChange={e => { setReduceMotion(e.target.checked); localStorage.setItem("cortex-reduce-motion", String(e.target.checked)); }}/> Reduce interface motion</label><p>This preference is saved on this device.</p></Panel><Panel title="Connection details"><p>{connected ? "The last snapshot was received successfully." : "The hub is not connected. No successful execution is assumed."}</p><p>Last sync: {syncedAt?.toLocaleTimeString() || "Never"} · Hub round trip: {latency === null ? "Not measured" : `${latency} ms`}</p><button className="control-button" onClick={() => refreshRef.current()}>Refresh connection</button></Panel></>}
      </div></main>
    <aside className="intel-rail"><section className="rail-section"><div className="section-heading"><div><p className="eyebrow">DEVICE MESH</p><h3>Command target</h3></div><button aria-label="Open devices" onClick={() => go("Devices")}><Icon name="chevron"/></button></div>{devices.map(d => <button className={`device-row ${selected === d.device_id ? "selected" : ""}`} key={d.device_id} onClick={() => setSelected(d.device_id)}><span className="device-icon"><Icon name={d.device_type === "phone" ? "phone" : "monitor"}/></span><span className="device-name"><strong>{d.display_name || d.device_id}</strong><small>{d.platform || d.device_type || "Enrolled device"}</small></span><span className="device-status"><i className={connected && d.online ? "online" : "offline"}/><small>{connected && d.online ? "Online" : "Offline"}</small></span></button>)}{!devices.length && <Empty>No connected device data.</Empty>}</section><section className="rail-section"><div className="section-heading"><div><p className="eyebrow">LIVE FEED</p><h3>Recent activity</h3></div><button className="text-button" onClick={() => go("Activity")}>All</button></div>{snapshot.activities.slice(0, 3).map((a, i) => <div className="rail-event" key={i}><strong>{a.label}</strong><small>{date(a.timestamp)}</small></div>)}{!snapshot.activities.length && <Empty>No activity received.</Empty>}</section><section className="rail-section quick-card"><p className="eyebrow">QUICK CONTROLS</p><div className="quick-grid">{[["Saved memory", "Memory", "brain"], ["Task manager", "Automations", "bolt"], ["Approvals", "Security", "shield"], ["Runtime", "Settings", "settings"]].map(([label, target, icon]) => <button key={label} onClick={() => go(target)}><Icon name={icon}/><span>{label}</span></button>)}</div></section></aside>
  </div>;
}
