"""One durable request owner covers preparation, reasoning and final delivery."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os

from .intent_schema import ConversationalResponse

log = logging.getLogger("cortex.requests")


@dataclass
class RequestJob:
    device: str
    task: asyncio.Task
    text_digest: str
    acknowledged: bool = False


class RequestLifecycle:
    # Keep the configured acknowledgement below the desktop transport's 8s budget.
    ACK_SECONDS = min(5, max(.01, float(os.getenv("CORTEX_REQUEST_ACK_SECONDS", "3"))))
    PREPARATION_SECONDS = float(os.getenv("CORTEX_PREPARATION_TIMEOUT", "30"))
    _jobs = {}

    @classmethod
    async def submit(cls, device, request_id, text, prepare, harness, channel="gui", deliver=None,
                     latency_metrics=None, log_action_callback=None):
        digest = hashlib.sha256(text.encode()).hexdigest()
        task_id = "request_" + hashlib.sha256((device + "\0" + request_id).encode()).hexdigest()[:24]
        existing = cls._jobs.get(task_id)
        if existing and existing.text_digest != digest:
            return ConversationalResponse(status="error", message="That request identifier was already used for different input.")
        if existing is None:
            # No database/context/model await precedes the acknowledgement budget.
            job = asyncio.create_task(cls._work(task_id, device, request_id, text, digest, prepare,
                harness, channel, latency_metrics, log_action_callback))
            existing = RequestJob(device, job, digest)
            cls._jobs[task_id] = existing
            job.add_done_callback(lambda done: cls._completed(task_id, done, deliver))
        try:
            return await asyncio.wait_for(asyncio.shield(existing.task), cls.ACK_SECONDS)
        except asyncio.TimeoutError:
            existing.acknowledged = True
            return ConversationalResponse(status="in_progress",
                message="Your request is being processed. I'll report the result when it finishes.",
                data={"task_id": task_id}, command_id=None)

    @classmethod
    def _completed(cls, task_id, job, deliver):
        record = cls._jobs.pop(task_id, None)
        if record is None or not record.acknowledged or deliver is None or job.cancelled():
            return
        try:
            result = job.result()
        except Exception:
            log.warning("request_delivery_unavailable task_id=%s", task_id)
            return
        notification = asyncio.create_task(deliver(task_id, result))
        # Keep delivery owned until complete and consume its exception.
        cls._deliveries.add(notification)
        notification.add_done_callback(cls._delivery_done)

    _deliveries = set()

    @classmethod
    def _delivery_done(cls, job):
        cls._deliveries.discard(job)
        if not job.cancelled() and job.exception():
            log.warning("request_delivery_failed reason=%s result_is_persisted=true", type(job.exception()).__name__)

    @classmethod
    async def _work(cls, task_id, device, request_id, text, digest, prepare, harness, channel, metrics, log_action):
        from .database import add_task, get_task, update_task
        from .session_context import SessionContextManager
        saved = {"request_id": request_id, "request": text, "input_digest": digest,
                 "response_channel": channel, "runtime": "request_lifecycle"}
        persisted = False
        stage = "preparation"
        try:
            previous = await get_task(task_id)
            if previous:
                old = previous.get("context") or {}
                old = json.loads(old) if isinstance(old, str) else old
                if old.get("input_digest") != digest:
                    return ConversationalResponse(status="error", message="That request identifier was already used for different input.")
                if old.get("result"):
                    result = dict(old["result"])
                    result.pop("task_id", None)
                    return ConversationalResponse.model_validate(result)
                # Never replay a request that may have executed before a restart.
                return ConversationalResponse(status="error", message="This request was already accepted. Completion is unverified; it was not replayed.",
                                              data={"task_id": task_id})
            else:
                await add_task({"id": task_id, "title": text[:80], "type": "request",
                    "status": "RUNNING", "assigned_device": device, "command": "request", "context": saved})
                persisted = True
                async with asyncio.timeout(cls.PREPARATION_SECONDS):
                    context = await prepare()
                context = dict(context, response_channel=channel, _request_delivery_owned=True,
                               _request_task_id=task_id)
                stage = "execution"
                result = await harness.process_request(source_device_id=device, natural_request=text, context=context,
                    request_id=request_id, latency_metrics=metrics, log_action_callback=log_action)
                if not isinstance(result, ConversationalResponse):
                    result = ConversationalResponse.model_validate(result, from_attributes=True)
                if result.status == "in_progress":
                    result = ConversationalResponse(status="clarification_needed", message=result.message,
                        data={"active_task_id": (result.data or {}).get("task_id")})
        except asyncio.CancelledError:
            result = ConversationalResponse(status="cancelled", message="The request was cancelled. An action already sent may still finish.")
        except TimeoutError:
            result = ConversationalResponse(status="error",
                message="Request preparation timed out. No new action was started." if stage == "preparation"
                    else "The request timed out. An action already sent may still finish; check Activity before retrying.",
                data={"error_code": "request_preparation_timeout" if stage == "preparation" else "request_execution_timeout"})
        except Exception as exc:
            log.warning("request_failed task_id=%s reason=%s", task_id, type(exc).__name__)
            result = ConversationalResponse(status="error", message="The request failed before completion could be verified.")
        result.data = dict(result.data or {}, task_id=task_id)
        payload = dict(result.model_dump(), task_id=task_id)
        if persisted:
            try:
                await update_task(task_id, {"status": "COMPLETED" if result.status == "success" else
                    "CANCELLED" if result.status == "cancelled" else "FAILED",
                    "context": dict(saved, result=payload)})
            except Exception as exc:
                log.warning("request_persistence_failed task_id=%s reason=%s", task_id, type(exc).__name__)
                return ConversationalResponse(status="error", message="The request ended, but its outcome could not be saved. Do not retry an action without checking its state.",
                                              data={"task_id": task_id, "error_code": "request_persistence_failed"})
            try:
                async with asyncio.timeout(2):
                    await SessionContextManager.update_session(device, text, payload)
            except Exception as exc:
                # A saved execution outcome stays authoritative if history refresh fails.
                log.warning("request_history_update_failed task_id=%s reason=%s", task_id, type(exc).__name__)
        return result

    @classmethod
    def cancel_task(cls, task_id):
        record = cls._jobs.get(task_id)
        if record and record.task is not asyncio.current_task():
            record.task.cancel()

    @classmethod
    def cancel_for_device(cls, device):
        for record in list(cls._jobs.values()):
            if record.device == device and record.task is not asyncio.current_task():
                record.task.cancel()

    @classmethod
    def cancel_all(cls):
        for record in list(cls._jobs.values()):
            if record.task is not asyncio.current_task():
                record.task.cancel()

    @classmethod
    async def shutdown(cls):
        jobs = [record.task for record in list(cls._jobs.values())]
        cls.cancel_all()
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*jobs, return_exceptions=True)
                await asyncio.gather(*list(cls._deliveries), return_exceptions=True)
        except TimeoutError:
            log.warning("request_shutdown_incomplete restart_reconciliation_required=true")
