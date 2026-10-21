import test from "node:test";
import assert from "node:assert/strict";
import { pollTaskResults, isActiveTask, isTerminalStatus } from "../agent/ui/task-sync.js";

const message = { id: "chat", task_id: "turn", status: "in_progress", source: "node" };
const task = { id: "turn", assigned_device: "node", status: "COMPLETED", context: JSON.stringify({
  result: { status: "success", task_id: "turn", message: "The actual final answer." },
}) };

test("saved completion replaces a background acknowledgement", async () => {
  const updates = await pollTaskResults([message], async (method, path) => {
    assert.equal(method, "GET"); assert.equal(path, "/tasks/turn"); return task;
  });
  assert.equal(updates[0].status, "success");
  assert.equal(updates[0].response, "The actual final answer.");
});

test("a failed task fetch preserves pending status and exposes sync failure", async () => {
  const [update] = await pollTaskResults([message], async () => { throw new Error("HTTP 503"); });
  assert.equal(update.status, undefined);
  assert.match(update.sync_error, /HTTP 503/);
});

test("a later poll recovers after a fetch failure", async () => {
  await pollTaskResults([message], async () => { throw new Error("offline"); });
  const [update] = await pollTaskResults([message], async () => task);
  assert.equal(update.sync_error, "");
  assert.equal(update.status, "success");
});

test("another task or source cannot replace this answer", async () => {
  for (const bad of [{ ...task, id: "other" }, { ...task, assigned_device: "other" },
      { ...task, context: { result: { status: "success", task_id: "other", message: "wrong" } } }]) {
    const [update] = await pollTaskResults([message], async () => bad);
    assert.ok(update.sync_error);
    assert.equal(update.response, undefined);
  }
});

test("stored failures remain failures rather than success", async () => {
  const [update] = await pollTaskResults([message], async () => ({ ...task, status: "FAILED",
    context: { result: { status: "error", message: "Provider timed out." } } }));
  assert.equal(update.status, "error");
  assert.equal(update.response, "Provider timed out.");
});

test("clarification is a finished response, not ongoing work", async () => {
  const [update] = await pollTaskResults([message], async () => ({ ...task,
    context: { result: { status: "clarification_needed", message: "Which device?" } } }));
  assert.equal(update.response, "Which device?");
  assert.ok(isTerminalStatus(update.status));
});

test("completed rows and future schedules are not counted as active", () => {
  for (const status of ["COMPLETED", "FAILED", "pending", "paused", "clarification_needed", "interrupted"])
    assert.equal(isActiveTask({ status }), false);
  assert.ok(isActiveTask({ status: "RUNNING" }));
});

test("execution steps do not double-count their parent task", () => {
  const rows = [{ id: "turn", status: "RUNNING" },
    { id: "step", parent_task_id: "turn", status: "ACTIVE" }];
  assert.equal(rows.filter(row => isActiveTask(row, rows)).length, 1);
});

test("a terminal task without an answer does not fabricate one", async () => {
  const [update] = await pollTaskResults([message], async () => ({ ...task, context: "{}" }));
  assert.match(update.response, /without a verified response/);
});

test("a waiting approval remains visible and pollable", async () => {
  const [update] = await pollTaskResults([message], async () => ({ ...task,
    status: "WAITING", context: "{}", last_error: "Waiting for cloud-sharing approval" }));
  assert.equal(update.status, "waiting");
  assert.match(update.response, /approval/);
  assert.equal(isTerminalStatus(update.status), false);
});
