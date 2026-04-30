from fastapi import APIRouter, Request, HTTPException, Depends, UploadFile, File, Form
from typing import Optional
from datetime import datetime
from bson import ObjectId
import cloudinary, cloudinary.uploader, os

from app.models import LostFoundCreate
from app.deps import get_current_user, get_db

router = APIRouter()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)


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
        "date_lost_found": r["date_lost_found"].isoformat() if r.get("date_lost_found") else None,
        "contact_email": r.get("contact_email"),
        "created_at": r["created_at"].isoformat(),
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


@router.post("/{report_id}/images")
async def upload_report_image(
    report_id: str,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    except Exception:
        raise HTTPException(400, "Invalid report id")

    if not r:
        raise HTTPException(404, "Report not found")
    if r["reported_by_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your report")

    contents = await file.read()

    try:
        result = cloudinary.uploader.upload(
            contents,
            folder="campusorbit/lost_found",
            resource_type="image",
        )
        image_url = result["secure_url"]
    except Exception as e:
        raise HTTPException(500, f"Image upload failed: {str(e)}")

    await db.lost_found.update_one(
        {"_id": r["_id"]},
        {"$push": {"images": image_url}},
    )
    return {"success": True, "url": image_url}


@router.get("")
async def get_reports(
    request: Request,
    type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    category: Optional[str] = None,
):
    db = get_db(request)
    query: dict = {}

    if type and type in ("lost", "found"):
        query["type"] = type
    if status:
        query["status"] = status
    # Default: show open items only when status not specified
    if not status:
        query["status"] = "open"
    if category:
        query["category"] = {"$regex": category, "$options": "i"}
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"location": {"$regex": search, "$options": "i"}},
        ]

    reports = await db.lost_found.find(query).sort("created_at", -1).to_list(100)
    result = []
    for r in reports:
        reporter = await db.users.find_one({"_id": r["reported_by_id"]}) or {}
        result.append(fmt_report(r, reporter))
    return {"success": True, "reports": result}


@router.get("/my")
async def my_reports(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    reports = await db.lost_found.find(
        {"reported_by_id": current_user["_id"]}
    ).sort("created_at", -1).to_list(100)

    result = []
    for r in reports:
        result.append(fmt_report(r, current_user))
    return {"success": True, "reports": result}


@router.get("/{report_id}")
async def get_report(report_id: str, request: Request):
    db = get_db(request)
    try:
        r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    except Exception:
        raise HTTPException(400, "Invalid report id")

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
    try:
        r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    except Exception:
        raise HTTPException(400, "Invalid report id")

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


@router.delete("/{report_id}")
async def delete_report(
    report_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    try:
        r = await db.lost_found.find_one({"_id": ObjectId(report_id)})
    except Exception:
        raise HTTPException(400, "Invalid report id")

    if not r:
        raise HTTPException(404, "Report not found")
    if r["reported_by_id"] != current_user["_id"]:
        raise HTTPException(403, "Not your report")

    await db.lost_found.delete_one({"_id": r["_id"]})
    return {"success": True}
