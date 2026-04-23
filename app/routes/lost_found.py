from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime
from bson import ObjectId

from app.models import LostFoundCreate
from app.deps import get_current_user, get_db

router = APIRouter()


def fmt_report(r: dict, reporter: dict) -> dict:
    return {
        "id": str(r["_id"]),
        "reported_by_id": str(r["reported_by_id"]),
        "reported_by_name": reporter.get("name", ""),
        "type": r["type"],
        "title": r["title"],
        "description": r["description"],
        "category": r.get("category"),
        "images": r.get("images", []),
        "location": r["location"],
        "status": r["status"],
        "reward": r.get("reward", 0),
        "date_lost_found": r.get("date_lost_found"),
        "contact_email": r.get("contact_email"),
        "created_at": r["created_at"],
    }


@router.post("", status_code=201)
async def create_report(
    body: LostFoundCreate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    doc = {
        "reported_by_id": current_user["_id"],
        "type": body.type,
        "title": body.title,
        "description": body.description,
        "category": body.category,
        "images": [],
        "location": body.location,
        "date_lost_found": body.date_lost_found,
        "contact_email": body.contact_email or current_user.get("email"),
        "reward": body.reward,
        "status": "open",
        "created_at": datetime.utcnow(),
    }
    result = await db.lost_found.insert_one(doc)
    return {"success": True, "report_id": str(result.inserted_id)}


@router.get("")
async def get_reports(request: Request, type: str = None, status: str = "open"):
    db = get_db(request)
    query = {}
    if type:
        query["type"] = type
    if status:
        query["status"] = status

    reports = await db.lost_found.find(query).sort("created_at", -1).to_list(100)
    result = []
    for r in reports:
        reporter = await db.users.find_one({"_id": r["reported_by_id"]}) or {}
        result.append(fmt_report(r, reporter))
    return {"success": True, "reports": result}


@router.get("/{report_id}")
async def get_report(report_id: str, request: Request):
    db = get_db(request)
    r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    if not r:
        raise HTTPException(404, "Report not found")
    reporter = await db.users.find_one({"_id": r["reported_by_id"]}) or {}
    return {"success": True, "report": fmt_report(r, reporter)}


@router.patch("/{report_id}/resolve")
async def resolve_report(
    report_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    if not r:
        raise HTTPException(404, "Report not found")
    if r["reported_by_id"] != current_user["_id"]:
        raise HTTPException(403, "Only reporter can resolve")
    if r["status"] == "resolved":
        raise HTTPException(400, "Already resolved")

    await db.lost_found.update_one(
        {"_id": r["_id"]},
        {"$set": {"status": "resolved", "resolved_at": datetime.utcnow()}},
    )
    return {"success": True}