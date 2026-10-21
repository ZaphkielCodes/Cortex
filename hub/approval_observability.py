"""Read-only metadata for live approval windows; never grants authorization."""
from datetime import datetime, timedelta, timezone

_windows = {}


def publish(command_id, seconds, *, task_id, parent_task_id, recipient, model, data_class, reason):
    now = datetime.now(timezone.utc)
    row = dict(created_at=now.isoformat(), expires_at=(now+timedelta(seconds=seconds)).isoformat(),
               task_id=task_id, parent_task_id=parent_task_id, recipient=recipient, model=model,
               data_class=data_class, approval_reason=reason)
    _windows[command_id] = row
    while len(_windows) > 128:
        _windows.pop(next(iter(_windows)))
    return dict(row)


def finish(command_id, outcome):
    if command_id in _windows:
        _windows[command_id]["runtime_outcome"] = outcome


def snapshot(command_id):
    return dict(_windows.get(command_id, {}))
