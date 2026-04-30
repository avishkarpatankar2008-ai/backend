from fastapi import APIRouter, Request, HTTPException, Depends, Query, UploadFile, File
from typing import Optional, List
from datetime import datetime, timedelta
from bson import ObjectId
import asyncio
import cloudinary, cloudinary.uploader, os

from app.models import ItemCreate, ItemUpdate, ItemOut
from app.deps import get_current_user, get_db

router = APIRouter()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)


def fmt_item(item: dict, owner: dict) -> dict:
    return {
        "id": str(item["_id"]),
        "owner_id": str(item["owner_id"]),
        "owner_name": owner.get("name", ""),
        "owner_avg_rating": owner.get("avg_rating", 0.0),
        "owner_hostel_block": owner.get("hostel_block"),
        "owner_trust_score": owner.get("trust_score", 50),
        "title": item["title"],
        "description": item.get("description"),
        "category": item["category"],
        "condition": item["condition"],
        "images": item.get("images", []),
        "price_per_day": item["price_per_day"],
        "security_deposit": item.get("security_deposit", 0),
        "max_rental_days": item.get("max_rental_days", 30),
        "is_available": item.get("is_available", True),
        "instant_booking": item.get("instant_booking", False),
        "barter_ok": item.get("barter_ok", False),
        "location_name": item.get("location_name", ""),
        "avg_rating": item.get("avg_rating", 0.0),
        "views": item.get("views", 0),
        "tags": item.get("tags", []),
        "created_at": item["created_at"],
    }


# ── My listings ─────────────────────────────────────────────────────────────
# IMPORTANT: Must be defined BEFORE /{item_id} to avoid route conflict.

@router.get("/my/listings")
async def my_listings(request: Request, current_user: dict = Depends(get_current_user)):
    db = get_db(request)
    items = await db.items.find({"owner_id": current_user["_id"]}).sort("created_at", -1).to_list(100)
    return {"success": True, "items": [fmt_item(i, current_user) for i in items]}


# ── Browse items ──────────────────────────────────────────────────────────────

@router.get("")
async def get_items(
    request: Request,
    category: Optional[str] = None,
    condition: Optional[str] = None,
    max_price: Optional[float] = None,
    search: Optional[str] = None,
    hostel_block: Optional[str] = None,
    instant_booking: Optional[bool] = None,
    barter_ok: Optional[bool] = None,
    last_minute: Optional[bool] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    sort: str = "newest",
    page: int = 1,
    limit: int = 20,
):
    db = get_db(request)
    query: dict = {"is_available": True}

    if category:
        query["category"] = category
    if condition:
        query["condition"] = condition
    if max_price:
        query["price_per_day"] = {"$lte": max_price}
    if search:
        query["$text"] = {"$search": search}
    if hostel_block:
        query["location_name"] = {"$regex": hostel_block, "$options": "i"}
    if instant_booking:
        query["instant_booking"] = True
    if barter_ok:
        query["barter_ok"] = True
    if last_minute:
        cutoff = datetime.utcnow() + timedelta(days=2)
        query["available_from"] = {"$lte": cutoff}

    sort_map = {
        "newest": [("created_at", -1)],
        "price_asc": [("price_per_day", 1)],
        "price_desc": [("price_per_day", -1)],
        "rating": [("avg_rating", -1)],
    }
    sort_by = sort_map.get(sort, [("created_at", -1)])

    # FIX: $near cannot be combined with .sort(); use it only when no sort needed.
    # When geo search is requested we skip the sort (proximity IS the sort).
    if lat is not None and lng is not None:
        geo_query = {
            **query,
            "location": {
                "$near": {
                    "$geometry": {"type": "Point", "coordinates": [lng, lat]},
                    "$maxDistance": 2000,
                }
            },
        }
        total = await db.items.count_documents(query)  # count without geo for perf
        cursor = db.items.find(geo_query).skip((page - 1) * limit).limit(limit)
    else:
        total = await db.items.count_documents(query)
        cursor = db.items.find(query).sort(sort_by).skip((page - 1) * limit).limit(limit)

    items = await cursor.to_list(limit)

    result = []
    for item in items:
        owner = await db.users.find_one({"_id": item["owner_id"]}) or {}
        result.append(fmt_item(item, owner))

    return {"success": True, "total": total, "pages": -(-total // limit), "items": result}


# ── Create item ───────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_item(
    body: ItemCreate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    tags = list(set(
        [w for w in f"{body.title} {body.category} {body.description or ''}".lower().split() if len(w) > 2]
    ))
    doc = {
        "owner_id": current_user["_id"],
        "title": body.title,
        "description": body.description,
        "category": body.category,
        "condition": body.condition,
        "images": [],
        "price_per_day": body.price_per_day,
        "security_deposit": body.security_deposit,
        "max_rental_days": body.max_rental_days,
        "location_name": body.location_name,
        "location": {"type": "Point", "coordinates": [body.lng or 0, body.lat or 0]},
        "is_available": True,
        "instant_booking": body.instant_booking,
        "barter_ok": body.barter_ok,
        "available_from": datetime.utcnow(),
        "avg_rating": 0.0,
        "views": 0,
        "tags": tags,
        "created_at": datetime.utcnow(),
    }
    result = await db.items.insert_one(doc)
    await db.users.update_one({"_id": current_user["_id"]}, {"$inc": {"total_items_listed": 1}})
    return {"success": True, "item_id": str(result.inserted_id)}


# ── Single item ───────────────────────────────────────────────────────────────

@router.get("/{item_id}")
async def get_item(item_id: str, request: Request):
    db = get_db(request)
    try:
        oid = ObjectId(item_id)
    except Exception:
        raise HTTPException(400, "Invalid item id")
    item = await db.items.find_one({"_id": oid})
    if not item:
        raise HTTPException(404, "Item not found")
    await db.items.update_one({"_id": item["_id"]}, {"$inc": {"views": 1}})
    owner = await db.users.find_one({"_id": item["owner_id"]}) or {}
    return {"success": True, "item": fmt_item(item, owner)}


# ── Upload images ─────────────────────────────────────────────────────────────

@router.post("/{item_id}/images")
async def upload_images(
    item_id: str,
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(item_id)
    except Exception:
        raise HTTPException(400, "Invalid item id")
    item = await db.items.find_one({"_id": oid})
    if not item or item["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your item")

    loop = asyncio.get_running_loop()
    urls = []
    for f in files:
        data = await f.read()
        # FIX: Cloudinary upload is blocking — run in executor to avoid blocking the event loop
        res = await loop.run_in_executor(
            None,
            lambda d=data: cloudinary.uploader.upload(d, folder="campusorbit/items"),
        )
        urls.append(res["secure_url"])

    await db.items.update_one({"_id": item["_id"]}, {"$push": {"images": {"$each": urls}}})
    return {"success": True, "urls": urls}


# ── Update item ───────────────────────────────────────────────────────────────

@router.put("/{item_id}")
async def update_item(
    item_id: str,
    body: ItemUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(item_id)
    except Exception:
        raise HTTPException(400, "Invalid item id")
    item = await db.items.find_one({"_id": oid})
    if not item:
        raise HTTPException(404, "Item not found")
    if item["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your item")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        await db.items.update_one({"_id": item["_id"]}, {"$set": updates})
    return {"success": True}


# ── Delete item ───────────────────────────────────────────────────────────────

@router.delete("/{item_id}")
async def delete_item(
    item_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(item_id)
    except Exception:
        raise HTTPException(400, "Invalid item id")
    item = await db.items.find_one({"_id": oid})
    if not item:
        raise HTTPException(404, "Item not found")
    if item["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your item")

    await db.items.delete_one({"_id": item["_id"]})
    return {"success": True}


# ── Toggle availability ────────────────────────────────────────────────────────

@router.patch("/{item_id}/availability")
async def toggle_availability(
    item_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(item_id)
    except Exception:
        raise HTTPException(400, "Invalid item id")
    item = await db.items.find_one({"_id": oid})
    if not item or item["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your item")

    new_val = not item.get("is_available", True)
    await db.items.update_one({"_id": item["_id"]}, {"$set": {"is_available": new_val}})
    return {"success": True, "is_available": new_val}
