from fastapi import APIRouter, Depends, HTTPException, Query

from app.config.db import mongo
from app.config.settings import Settings, get_settings

router = APIRouter(prefix="/admin", tags=["admin"])


def require_db():
    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")
    return mongo.db


def require_admin(settings: Settings, phone: str):
    if phone not in settings.admin_numbers:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/messages")
async def list_messages(
    phone: str = Query(..., description="Admin phone (must be whitelisted)"),
    limit: int = Query(20, ge=1, le=200),
    settings: Settings = Depends(get_settings),
    db=Depends(require_db),
):
    require_admin(settings, phone)
    cursor = db.messages.find().sort("ts", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    # Mask ObjectId for JSON friendliness
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"messages": docs}


@router.get("/members")
async def list_members(
    phone: str = Query(..., description="Admin phone (must be whitelisted)"),
    limit: int = Query(20, ge=1, le=200),
    settings: Settings = Depends(get_settings),
    db=Depends(require_db),
):
    require_admin(settings, phone)
    cursor = db.members.find().sort("join_date", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"members": docs}


@router.get("/orders/summary")
async def orders_summary(
    phone: str = Query(..., description="Admin phone (must be whitelisted)"),
    settings: Settings = Depends(get_settings),
    db=Depends(require_db),
):
    require_admin(settings, phone)
    pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    agg = await db.orders.aggregate(pipeline).to_list(length=None)
    return {"summary": agg}


@router.get("/broadcasts")
async def list_broadcasts(
    phone: str = Query(..., description="Admin phone (must be whitelisted)"),
    limit: int = Query(20, ge=1, le=200),
    settings: Settings = Depends(get_settings),
    db=Depends(require_db),
):
    require_admin(settings, phone)
    cursor = db.broadcasts.find().sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"broadcasts": docs}


@router.get("/message-status")
async def message_status(
    phone: str = Query(..., description="Admin phone (must be whitelisted)"),
    limit: int = Query(50, ge=1, le=500),
    settings: Settings = Depends(get_settings),
    db=Depends(require_db),
):
    require_admin(settings, phone)
    cursor = db.message_status.find().sort("ts", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"statuses": docs}
