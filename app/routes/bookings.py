from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime
from bson import ObjectId

from app.models import BookingCreate, RatingIn
from app.deps import get_current_user, get_db

router = APIRouter()


def fmt_booking(b: dict, item: dict, renter: dict, owner: dict) -> dict:
    return {
        "id": str(b["_id"]),
        "item_id": str(b["item_id"]),
        "item_title": item.get("title", ""),
        "renter_id": str(b["renter_id"]),
        "renter_name": renter.get("name", ""),
        "owner_id": str(b["owner_id"]),
        "owner_name": owner.get("name", ""),
        "start_date": b["start_date"],
        "end_date": b["end_date"],
        "total_days": b["total_days"],
        "total_cost": b["total_cost"],
        "status": b["status"],
        "owner_rating": b.get("owner_rating"),
        "renter_rating": b.get("renter_rating"),
        "created_at": b["created_at"],
    }


async def _resolve(db, booking: dict):
    item   = await db.items.find_one({"_id": booking["item_id"]}) or {}
    renter = await db.users.find_one({"_id": booking["renter_id"]}) or {}
    owner  = await db.users.find_one({"_id": booking["owner_id"]}) or {}
    return fmt_booking(booking, item, renter, owner)


# ── IMPORTANT: Static/prefixed routes must come before /{booking_id} ─────────

# ── My lent items (as owner) ──────────────────────────────────────────────────

@router.get("/lent")
async def lent_bookings(request: Request, current_user: dict = Depends(get_current_user)):
    db = get_db(request)
    bookings = await db.bookings.find({"owner_id": current_user["_id"]}).sort("created_at", -1).to_list(100)
    return {"success": True, "bookings": [await _resolve(db, b) for b in bookings]}


# ── My bookings (as renter) ───────────────────────────────────────────────────

@router.get("")
async def my_bookings(request: Request, current_user: dict = Depends(get_current_user)):
    db = get_db(request)
    bookings = await db.bookings.find({"renter_id": current_user["_id"]}).sort("created_at", -1).to_list(100)
    return {"success": True, "bookings": [await _resolve(db, b) for b in bookings]}


# ── Create booking ────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_booking(
    body: BookingCreate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        item_oid = ObjectId(body.item_id)
    except Exception:
        raise HTTPException(400, "Invalid item_id format")

    item = await db.items.find_one({"_id": item_oid})

    if not item:
        raise HTTPException(404, "Item not found")
    if not item.get("is_available"):
        raise HTTPException(400, "Item is not available")
    if item["owner_id"] == current_user["_id"]:
        raise HTTPException(400, "Cannot book your own item")
    if body.start_date >= body.end_date:
        raise HTTPException(400, "end_date must be after start_date")

    # Check for overlapping active/approved bookings
    conflict = await db.bookings.find_one({
        "item_id": item["_id"],
        "status": {"$in": ["approved", "active"]},
        "start_date": {"$lt": body.end_date},
        "end_date": {"$gt": body.start_date},
    })
    if conflict:
        raise HTTPException(400, "Item already booked for those dates")

    total_days = max(1, (body.end_date - body.start_date).days)
    total_cost = total_days * item["price_per_day"] + item.get("security_deposit", 0)

    # instant_booking → skip pending, go straight to approved
    initial_status = "approved" if item.get("instant_booking") else "pending"

    doc = {
        "item_id": item["_id"],
        "renter_id": current_user["_id"],
        "owner_id": item["owner_id"],
        "start_date": body.start_date,
        "end_date": body.end_date,
        "total_days": total_days,
        "total_cost": total_cost,
        "status": initial_status,
        "owner_rating": None,
        "renter_rating": None,
        "created_at": datetime.utcnow(),
    }
    result = await db.bookings.insert_one(doc)

    if initial_status == "approved":
        await db.items.update_one({"_id": item["_id"]}, {"$set": {"is_available": False}})

    return {"success": True, "booking_id": str(result.inserted_id), "status": initial_status}


# ── Single booking ────────────────────────────────────────────────────────────

@router.get("/{booking_id}")
async def get_booking(
    booking_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(400, "Invalid booking id")
    b = await db.bookings.find_one({"_id": oid})
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["renter_id"] != current_user["_id"] and b["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Access denied")
    return {"success": True, "booking": await _resolve(db, b)}


# ── Approve (owner) ───────────────────────────────────────────────────────────

@router.patch("/{booking_id}/approve")
async def approve_booking(
    booking_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(400, "Invalid booking id")
    b = await db.bookings.find_one({"_id": oid})
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Only owner can approve")
    if b["status"] != "pending":
        raise HTTPException(400, f"Cannot approve a booking with status '{b['status']}'")

    await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": "approved"}})
    await db.items.update_one({"_id": b["item_id"]}, {"$set": {"is_available": False}})
    return {"success": True}


# ── Mark returned ─────────────────────────────────────────────────────────────

@router.patch("/{booking_id}/return")
async def return_booking(
    booking_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(400, "Invalid booking id")
    b = await db.bookings.find_one({"_id": oid})
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Only owner can mark as returned")
    if b["status"] not in ("approved", "active"):
        raise HTTPException(400, "Booking is not active")

    await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": "returned"}})
    await db.items.update_one({"_id": b["item_id"]}, {"$set": {"is_available": True}})
    return {"success": True}


# ── Cancel ────────────────────────────────────────────────────────────────────

@router.patch("/{booking_id}/cancel")
async def cancel_booking(
    booking_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(400, "Invalid booking id")
    b = await db.bookings.find_one({"_id": oid})
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["renter_id"] != current_user["_id"] and b["owner_id"] != current_user["_id"]:
        raise HTTPException(403, "Access denied")
    if b["status"] in ("returned", "cancelled"):
        raise HTTPException(400, "Cannot cancel")

    await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": "cancelled"}})
    # Re-open item if it was approved
    if b["status"] == "approved":
        await db.items.update_one({"_id": b["item_id"]}, {"$set": {"is_available": True}})
    return {"success": True}


# ── Rate ──────────────────────────────────────────────────────────────────────

@router.post("/{booking_id}/rate")
async def rate_booking(
    booking_id: str,
    body: RatingIn,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(400, "Invalid booking id")
    b = await db.bookings.find_one({"_id": oid})
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["status"] != "returned":
        raise HTTPException(400, "Can only rate after item is returned")

    uid = current_user["_id"]
    rating_doc = {"stars": body.stars, "comment": body.comment, "rated_at": datetime.utcnow()}

    if uid == b["renter_id"]:
        # Renter rates owner
        if b.get("owner_rating"):
            raise HTTPException(400, "Already rated")
        await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"owner_rating": rating_doc}})
        target_id = b["owner_id"]
    elif uid == b["owner_id"]:
        # Owner rates renter
        if b.get("renter_rating"):
            raise HTTPException(400, "Already rated")
        await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"renter_rating": rating_doc}})
        target_id = b["renter_id"]
    else:
        raise HTTPException(403, "Not part of this booking")

    # Recalculate avg_rating for target user
    target = await db.users.find_one({"_id": target_id})
    total = target.get("total_ratings", 0) + 1
    avg = ((target.get("avg_rating", 0) * (total - 1)) + body.stars) / total
    # Update trust_score: clamp between 0-100, +2 per 5-star, -5 per 1-star
    delta = 2 if body.stars == 5 else (-5 if body.stars == 1 else 0)
    new_trust = max(0, min(100, target.get("trust_score", 50) + delta))

    await db.users.update_one(
        {"_id": target_id},
        {"$set": {"avg_rating": round(avg, 2), "total_ratings": total, "trust_score": new_trust}},
    )
    return {"success": True}
