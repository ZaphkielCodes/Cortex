"""A12 authenticated presentation and tightly scoped task interactions."""
from fastapi import APIRouter, Depends, HTTPException, Request
from .workspace import WorkspaceStore, Cursor, ActionRequest, action_id, owned_tasks, state, TERMINAL

store=WorkspaceStore()


async def caller(request: Request):
    from .main import _authenticate_http_headers
    identity=await _authenticate_http_headers(request.headers.get('X-Cortex-Device'),
        request.headers.get('X-Cortex-Secret'),request.cookies.get('cortex_session'))
    if identity.get('role') not in {'admin','primary_admin'}:
        raise HTTPException(403,'Workspace controls require administrator permission')
    return identity


router=APIRouter(prefix='/api/workspace',tags=['workspace'])


@router.post('/sync')
async def sync(cursor: Cursor, identity=Depends(caller)):
    return await store.sync(identity['device_id'],cursor)


@router.post('/action')
async def action(body: ActionRequest, identity=Depends(caller)):
    source=identity['device_id']
    rows=await owned_tasks(source)
    row=next((r for r in rows if r['id']==body.task_id),None)
    if row is None:raise HTTPException(403,'This task does not belong to the requesting workspace')
    if state(row) in TERMINAL:raise HTTPException(409,'The task has already ended; refresh its state')
    if body.action_id!=action_id(source,row):raise HTTPException(409,'This control is stale; refresh its state')
    from .api_control import stop_task_endpoint
    # Reuse the existing lifecycle/cancellation path; never accept a command,
    # capability name, code, URL, approval decision, or arbitrary arguments.
    return await stop_task_endpoint(body.task_id)
