"""
hub/api_people.py
-----------------
Phase 7A: REST API for managing people, facts, and relationships.
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Header, HTTPException, Cookie
from pydantic import BaseModel

from .people import PeopleManager
from .database import (
    get_person,
    delete_person,
    update_person,
    add_person_fact,
    get_person_facts,
    delete_person_fact,
    add_relationship,
    get_relationships,
    list_people,
)
from .models import (
    PersonCreate,
    PersonUpdate,
    PersonFactCreate,
    RelationshipCreate,
)


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)


router = APIRouter(prefix="/people", tags=["people"])


class RememberRequest(BaseModel):
    name: str
    aliases: List[str] = []
    relationship_type: str = ""
    notes: str = ""
    key: Optional[str] = None
    value: Optional[str] = None
    confidence: float = 1.0
    source: str = "user"


@router.get("/")
async def list_or_search_people(
    query: Optional[str] = None,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    if query:
        results = await PeopleManager.search_people(query)
    else:
        results = await list_people()
        for p in results:
            p["facts"] = await get_person_facts(p["person_id"])
    return {"people": results}


@router.post("/")
async def remember_person_endpoint(
    req: RememberRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    result = await PeopleManager.remember_person(req.model_dump())
    if result.get("status") == "ambiguous":
        raise HTTPException(status_code=409, detail=result.get("message"))
    elif result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/{person_id}")
async def get_person_endpoint(
    person_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    person = await PeopleManager.get_person_details(person_id)
    if not person:
        raise HTTPException(status_code=404, detail=f"Person '{person_id}' not found.")
    return {"person": person}


@router.put("/{person_id}")
async def update_person_endpoint(
    person_id: str,
    updates: PersonUpdate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
    if not update_data:
        return {"status": "no changes"}
    success = await update_person(person_id, update_data)
    if not success:
        raise HTTPException(status_code=404, detail="Person not found")
    return {"status": "success"}


@router.delete("/{person_id}")
async def delete_person_endpoint(
    person_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    success = await delete_person(person_id)
    if not success:
        raise HTTPException(status_code=404, detail="Person not found")
    return {"status": "success"}


@router.get("/{person_id}/facts")
async def get_facts_endpoint(
    person_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    facts = await get_person_facts(person_id)
    return {"facts": facts}


@router.post("/{person_id}/facts")
async def add_fact_endpoint(
    person_id: str,
    fact: PersonFactCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    p = await get_person(person_id)
    if not p:
        raise HTTPException(status_code=404, detail="Person not found")
    fact_dict = fact.model_dump()
    fact_dict["person_id"] = person_id
    fact_id = await add_person_fact(fact_dict)
    return {"id": fact_id, "status": "success"}


@router.delete("/{person_id}/facts/{fact_id}")
async def delete_fact_endpoint(
    person_id: str,
    fact_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    success = await delete_person_fact(fact_id)
    if not success:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"status": "success"}


@router.post("/relationships")
async def add_relationship_endpoint(
    rel: RelationshipCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    rel_id = await add_relationship(rel.model_dump())
    return {"id": rel_id, "status": "success"}


@router.get("/{person_id}/relationships")
async def get_relationships_endpoint(
    person_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    relationships = await get_relationships(person_id)
    return {"relationships": relationships}
