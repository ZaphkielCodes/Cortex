// Reconcile durable task outcomes without depending on dashboard snapshot health.
const statusKey = value => String(value || "unknown").trim().toLowerCase().replaceAll("_", " ");

export const isTerminalStatus = value => new Set([
  "completed", "cancelled", "failed", "success", "error", "denied", "unsupported",
  "verification failed", "blocked", "clarification needed", "expired", "interrupted",
]).has(statusKey(value));

export function taskResult(task) {
  try { return (typeof task?.context === "string" ? JSON.parse(task.context) : task?.context)?.result || null; }
  catch { return null; }
}

export function isActiveTask(task, tasks = []) {
  if (task.parent_task_id && tasks.some(parent => parent.id === task.parent_task_id)) return false;
  return new Set(["active", "running", "in progress", "waiting", "waiting for executor",
    "handoff pending", "needs approval", "approval required", "dispatching", "resuming"]).has(statusKey(task.status));
}

export async function pollTaskResults(messages, request) {
  const pending = messages.filter(message => message.task_id && !isTerminalStatus(message.status)).slice(-10);
  return Promise.all(pending.map(async message => {
    try {
      const task = await request("GET", `/tasks/${encodeURIComponent(message.task_id)}`);
      if (!task || task.id !== message.task_id ||
          (message.source && task.assigned_device && task.assigned_device !== message.source)) {
        throw new Error("The returned task does not match this conversation.");
      }
      const result = taskResult(task);
      if (result?.task_id && result.task_id !== message.task_id) throw new Error("The task result has a different identifier.");
      if (result && isTerminalStatus(result.status)) {
        return { id: message.id, status: result.status, response: result.message || "The task ended without response text.", sync_error: "" };
      }
      if (isTerminalStatus(task.status)) {
        return { id: message.id, status: task.status,
          response: task.last_error || "This task ended without a verified response. Open Activity for details.", sync_error: "" };
      }
      return { id: message.id, ...(task.status === "WAITING" && task.last_error
        ? { status: "waiting", response: task.last_error } : {}), sync_error: "" };
    } catch (error) {
      return { id: message.id, sync_error: `Could not refresh this task: ${error.message || "connection unavailable"}` };
    }
  }));
}
