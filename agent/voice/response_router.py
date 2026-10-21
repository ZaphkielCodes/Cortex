"""Correlate normal replies and background completions on the authenticated hub socket."""
import logging

log = logging.getLogger("cortex.voice.responses")


def route_voice_response(data, active_state):
    """Resolve pending requests and return unsolicited speech to queue, at most once.

    Completion can race the acknowledgement. Buffer it until that acknowledgement
    associates the task with the originating voice request. Unknown and timed-out
    requests remain rejected rather than accepting arbitrary unsolicited IDs.
    """
    cmd_id = data.get("command_id")
    futures = active_state.setdefault("voice_futures", {})
    timed_out = active_state.get("timed_out_cmd_ids", set())
    tasks = active_state.setdefault("voice_background_tasks", {})
    early = active_state.setdefault("early_voice_task_results", {})
    completed = active_state.setdefault("completed_voice_tasks", {})

    def deliver(result, task_id):
        if result.get("status") == "in_progress":
            approval_id = (result.get("data") or {}).get("approval_id")
            if not approval_id:
                return []
            seen = active_state.setdefault("voice_approval_prompts", {})
            key = (task_id, approval_id)
            if key in seen:
                return []
            seen[key] = True
            while len(seen) > 256:
                seen.pop(next(iter(seen)))
            early.pop(task_id, None)
            return [result.get("message") or "Cloud sharing requires your approval."]
        tasks.pop(task_id, None)
        early.pop(task_id, None)
        completed[task_id] = True
        while len(completed) > 256:
            completed.pop(next(iter(completed)))
        status = result.get("status", "error")
        return [result.get("message") or ("Done." if status == "success" else "That task failed.")]

    if data.get("is_task_result"):
        task_id, request_id = data.get("task_id"), data.get("request_id")
        if not task_id or not request_id or task_id in completed or request_id in timed_out:
            return []
        if tasks.get(task_id) == request_id:
            return deliver(data, task_id)
        future = futures.get(request_id)
        if future is not None and not future.done():
            early[task_id] = data
            while len(early) > 32:
                early.pop(next(iter(early)))
            return []
        log.warning("Discarding uncorrelated task result task_id=%s", task_id)
        return []

    future = futures.get(cmd_id)
    if future is not None and not future.done():
        task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
        if task_id and data.get("status") == "in_progress":
            tasks[task_id] = cmd_id
            approval_id = (data.get("data") or {}).get("approval_id")
            if approval_id:
                seen = active_state.setdefault("voice_approval_prompts", {})
                seen[(task_id, approval_id)] = True
                while len(seen) > 256:
                    seen.pop(next(iter(seen)))
            while len(tasks) > 256:
                tasks.pop(next(iter(tasks)))
        future.set_result(data)
        waiting = early.get(task_id)
        if waiting and waiting.get("request_id") == cmd_id:
            return deliver(waiting, task_id)
        return []
    if cmd_id in timed_out:
        log.warning("Discarding late voice_response for timed out request")
        return []
    if data.get("is_post_approval"):
        return [data.get("message") or "The approved action returned a result."]
    log.warning("Discarding unmatched or late voice_response (cmd_id: %s)", str(cmd_id or "none")[:8])
    return []
