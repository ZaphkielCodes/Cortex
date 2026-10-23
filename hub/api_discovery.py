"""Read-only discovery API. Advertisements require authenticated device WebSockets."""
from fastapi import APIRouter, Depends, Request
from .discovery import candidates

router = APIRouter(prefix='/api/discovery', tags=['discovery'])


async def caller(request: Request):
    from .main import _authenticate_http_headers
    return await _authenticate_http_headers(request.headers.get('X-Cortex-Device'),
        request.headers.get('X-Cortex-Secret'),request.cookies.get('cortex_session'))


@router.get('')
async def snapshot(identity=Depends(caller)):
    return {'protocol_version':1,'candidates':await candidates(identity['device_id'])}
