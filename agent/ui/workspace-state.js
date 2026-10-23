// A12 protocol v1: data only, no executable expressions, URLs or commands.
const kinds = new Set(['card', 'table', 'progress', 'timeline', 'task', 'status', 'error']);
const statuses = new Set(['PENDING', 'RUNNING', 'WAITING', 'COMPLETED', 'FAILED', 'CANCELLED', 'PAUSED', 'DENIED', 'UNKNOWN']);
const id = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value);
const text = (value, max = 2000) => typeof value === 'string' && value.length <= max;
const object = value => value && typeof value === 'object' && !Array.isArray(value);
export const emptyWorkspace = () => ({ epoch: null, revision: 0, items: [] });
export const progressMode = status => status === 'COMPLETED' ? 'complete' :
  ['PENDING', 'RUNNING', 'WAITING'].includes(status) ? 'active' : 'stopped';
export function validateItem(item) {
  if (!object(item) || !kinds.has(item.kind) || !id(item.id) || !text(item.title, 240) ||
      !object(item.provenance) || !id(item.provenance.task_id) ||
      !['task_db', 'capability_receipt', 'execution_trace', 'capability_discovery'].includes(item.provenance.source)) return false;
  if (['card', 'status', 'error'].includes(item.kind)) return text(item.text);
  if (item.kind === 'progress') return statuses.has(item.status) && text(item.label) &&
    (item.value === null || typeof item.value === 'number' && Number.isFinite(item.value) && item.value >= 0 && item.value <= 1);
  if (item.kind === 'table') return Array.isArray(item.columns) && item.columns.length <= 6 && item.columns.every(c => text(c, 80)) &&
    Array.isArray(item.rows) && item.rows.length <= 20 && item.rows.every(row => Array.isArray(row) && row.length <= 6 && row.every(c => text(c)));
  if (item.kind === 'timeline') return Array.isArray(item.entries) && item.entries.length <= 32 &&
    item.entries.every(e => object(e) && text(e.at) && text(e.stage) && text(e.detail));
  return statuses.has(item.status) && text(item.description) && text(item.result) && Array.isArray(item.actions) && item.actions.length <= 1 &&
    item.actions.every(a => object(a) && a.kind === 'cancel_task' && /^[a-f0-9]{32}$/.test(a.action_id) &&
      id(a.task_id) && a.task_id === item.provenance.task_id);
}
export function applyEnvelope(previous, message) {
  if (!object(message) || message.protocol_version !== 1 || !id(message.epoch) ||
      !Number.isSafeInteger(message.revision) || message.revision < 0 || !['snapshot', 'delta'].includes(message.mode) ||
      !Array.isArray(message.items) || message.items.length > 128 || !message.items.every(validateItem) ||
      !Array.isArray(message.removed) || message.removed.length > 128 || !message.removed.every(id) ||
      new Set(message.items.map(i => i.id)).size !== message.items.length) throw new Error('Unsupported or invalid workspace data.');
  if (message.mode === 'delta' && (message.epoch !== previous.epoch || message.base_revision !== previous.revision)) throw new Error('Workspace revision changed. Refreshing the snapshot is required.');
  if (message.epoch === previous.epoch && message.revision < previous.revision) throw new Error('Stale workspace update.');
  const items = new Map(message.mode === 'snapshot' ? [] : previous.items.map(item => [item.id, item]));
  for (const removed of message.removed) items.delete(removed);
  for (const item of message.items) items.set(item.id, item);
  if (items.size > 128) throw new Error('Workspace item limit exceeded.');
  // Deltas append Map entries. Keep the newest task visible without scattering
  // its response/receipts among older tasks or changing stable component IDs.
  const groups = new Map();
  for (const item of items.values()) {
    const taskId = item.provenance.task_id;
    if (!groups.has(taskId)) groups.set(taskId, { at: 0, items: [] });
    const group = groups.get(taskId);
    group.at = Math.max(group.at, Date.parse(item.provenance.updated_at) || 0);
    group.items.push(item);
  }
  const order = ['task', 'card', 'status', 'progress', 'timeline', 'table', 'error'];
  const ordered = [...groups.values()].sort((a, b) => b.at - a.at)
    .flatMap(group => group.items.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)));
  return { epoch: message.epoch, revision: message.revision, items: ordered };
}
export function actionRequest(item, action) {
  if (!validateItem(item) || item.kind !== 'task' || !item.actions.includes(action) ||
      ['COMPLETED', 'FAILED', 'CANCELLED', 'DENIED'].includes(item.status)) throw new Error('This control is not available.');
  return { kind: 'cancel_task', task_id: action.task_id, action_id: action.action_id };
}
