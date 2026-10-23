import { useEffect, useRef, useState } from 'react';
import { actionRequest, applyEnvelope, emptyWorkspace, progressMode } from './workspace-state.js';

function WorkspaceItem({ item, enabled, busy, act }) {
  const source = <small className="workspace-provenance">{item.provenance.source.replaceAll('_', ' ')} · {item.provenance.task_id}</small>;
  if (item.kind === 'task') return <article className="workspace-item workspace-task">
    <header><h3>{item.title}</h3><strong>{item.status}</strong></header>
    {item.description && <p>{item.description}</p>}
    <details><summary>Task details</summary>{source}{item.result && <p className="workspace-result">{item.result}</p>}</details>
    {item.actions.map(action => <button key={action.action_id} className="control-button danger" disabled={!enabled || busy}
      onClick={() => act(item, action)}>Cancel task</button>)}
  </article>;
  if (item.kind === 'progress') return <section className="workspace-item"><h4>{item.title}: {item.status}</h4>
    {progressMode(item.status) !== 'stopped' && <progress aria-label={`Task progress ${item.provenance.task_id}`} max={1}
      {...(progressMode(item.status) === 'complete' ? { value: 1 } : item.value === null ? {} : { value: item.value })}/>}
    <p>{item.label}</p></section>;
  if (item.kind === 'timeline') return <details className="workspace-item"><summary>{item.title}</summary><ol>
    {item.entries.map((entry, i) => <li key={i}><strong>{entry.stage}</strong>{entry.detail && <span> · {entry.detail}</span>}{entry.at && <small>{entry.at}</small>}</li>)}</ol>{source}</details>;
  if (item.kind === 'table') return <section className="workspace-item"><h4>{item.title}</h4>{item.rows.length ?
    <div className="workspace-table"><table><thead><tr>{item.columns.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
      <tbody>{item.rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div> : <p>No capability receipts have been recorded.</p>}{source}</section>;
  return <section className="workspace-item" role={item.kind === 'error' ? 'alert' : undefined}><h4>{item.title}</h4><p>{item.text}</p>{source}</section>;
}

export default function DynamicWorkspace({ request, voiceState, voiceConnected }) {
  const [view, setView] = useState(emptyWorkspace);
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = useRef(null), action = useRef(null);
  useEffect(() => {
    let disposed = false, polling = false, acting = false, cursor = emptyWorkspace();
    async function sync() {
      if (disposed || polling) return;
      polling = true;
      try {
        const message = await request('POST', '/api/workspace/sync', { epoch: cursor.epoch, revision: cursor.revision });
        if (disposed) return;
        try { cursor = applyEnvelope(cursor, message); }
        catch (problem) { cursor = emptyWorkspace(); throw problem; }
        setView(cursor); setStatus('connected'); setError('');
      } catch (problem) {
        if (!disposed) { setStatus('disconnected'); setError(problem.message || 'Workspace connection unavailable.'); }
      } finally { polling = false; }
    }
    refresh.current = sync;
    action.current = async (item, descriptor) => {
      if (disposed || acting) return;
      acting = true; setBusy(true);
      try {
        await request('POST', '/api/workspace/action', actionRequest(item, descriptor));
        await sync(); // Acknowledgement does not establish task completion.
      } catch (problem) { if (!disposed) setError(problem.message || 'The action could not be confirmed. Refresh before retrying.'); }
      finally { acting = false; if (!disposed) setBusy(false); }
    };
    sync();
    const timer = setInterval(sync, 2000);
    return () => { disposed = true; clearInterval(timer); refresh.current = null; action.current = null; };
  }, [request]);
  return <section className="dynamic-workspace" aria-label="Dynamic workspace">
    <header className="workspace-heading"><div><h2>Your workspace</h2><p>Task state and results recorded by Cortex.</p></div>
      <button className="control-button" onClick={() => refresh.current?.()}>Refresh workspace</button></header>
    <p role="status">{status === 'loading' ? 'Loading workspace…' : status === 'connected' ? 'Workspace connected' : 'Workspace disconnected — displayed state may be stale.'}</p>
    <p>Voice: {voiceConnected ? String(voiceState || 'unknown') : 'agent not connected'}</p>
    {error && <p role="alert" className="inline-error">{error}</p>}
    {status !== 'loading' && !view.items.length && <p>No tasks have been recorded for this device. Send a request to start.</p>}
    {view.items.some(item => item.provenance.source === 'capability_discovery') && <details className="workspace-item">
      <summary>Discovered capabilities</summary>
      {view.items.filter(item => item.provenance.source === 'capability_discovery').map(item =>
        <WorkspaceItem key={item.id} item={item} enabled={false} busy={false} act={() => {}}/>)}</details>}
    <div aria-live="polite">{view.items.filter(item => item.provenance.source !== 'capability_discovery').map(item => <WorkspaceItem key={item.id} item={item} enabled={status === 'connected'} busy={busy}
      act={(value, descriptor) => action.current?.(value, descriptor)}/>)}</div>
  </section>;
}
