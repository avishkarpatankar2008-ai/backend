from fastapi import APIRouter, Request, HTTPException, Depends, Response
from datetime import datetime, timedelta
import random, os, re
from bson import ObjectId
import bcrypt
import jwt

from app.models import UserRegister, UserLogin, OTPVerify, UserOut, UserUpdate
from app.deps import get_current_user, get_db

# ── Router ────────────────────────────────────────────────────
router = APIRouter()

# ✅ Handle preflight (CORS fix)
@router.options("/{rest_of_path:path}")
async def options_handler():
    return Response(status_code=200)

# ── Config ────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "changeme_secret_32chars_minimum!!")
JWT_EXPIRE_DAYS = 30
COLLEGE_EMAIL_RE = re.compile(r"\.(edu|ac\.in)$")

# ── Helpers ───────────────────────────────────────────────────
def make_token(user_id: str) -> str:
    payload = {
        "id": user_id,
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def hash_password(pwd: str) -> str:
    return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()

def verify_password(pwd: str, hashed: str) -> bool:
    return bcrypt.checkpw(pwd.encode(), hashed.encode())

def send_otp_email(email: str, name: str, otp: str):
    import smtplib
    from email.mime.text import MIMEText

    host = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    port = int(os.getenv("EMAIL_PORT", 587))
    user = os.getenv("EMAIL_USER", "")
    pwd  = os.getenv("EMAIL_PASS", "")

    if not user or not pwd:
        print(f"[DEV] OTP for {email}: {otp}")
        return

    msg = MIMEText(f"Hi {name},\n\nYour CampusOrbit OTP is: {otp}\nExpires in 10 minutes.")
    msg["Subject"] = "CampusOrbit — Verify your email"
    msg["From"] = user
    msg["To"] = email

    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
    except Exception as e:
        print(f"Email error: {e}")

# ── Register ──────────────────────────────────────────────────
@router.post("/register", status_code=201)
async def register(body: UserRegister, request: Request):
    db = get_db(request)

    if not COLLEGE_EMAIL_RE.search(body.email):
        raise HTTPException(400, "Use your college email (.edu or .ac.in)")

    if await db.users.find_one({"email": body.email}):
        raise HTTPException(400, "Email already registered")

    otp = str(random.randint(100000, 999999))

    user_doc = {
        "name": body.name,
        "email": body.email,
        "password": hash_password(body.password),
        "branch": body.branch,
        "year": body.year,
        "stay_type": body.stay_type,
        "hostel_block": body.hostel_block,
        "profile_photo": None,
        "role": "student",
        "is_verified": False,
        "otp": {
            "code": otp,
            "expires_at": datetime.utcnow() + timedelta(minutes=10)
        },
        "trust_score": 50,
        "total_ratings": 0,
        "avg_rating": 0.0,
        "upi_id": None,
        "location": {"type": "Point", "coordinates": [0, 0]},
        "created_at": datetime.utcnow(),
    }

    result = await db.users.insert_one(user_doc)
    send_otp_email(body.email, body.name, otp)

    return {
        "success": True,
        "user_id": str(result.inserted_id),
        "message": "OTP sent to your college email"
    }

# ── Verify OTP ────────────────────────────────────────────────
@router.post("/verify-otp")
async def verify_otp(body: OTPVerify, request: Request):
    db = get_db(request)
    user = await db.users.find_one({"_id": ObjectId(body.user_id)})

    if not user:
        raise HTTPException(404, "User not found")

    otp_data = user.get("otp", {})

    if otp_data.get("code") != body.otp:
        raise HTTPException(400, "Invalid OTP")

    if otp_data.get("expires_at", datetime.min) < datetime.utcnow():
        raise HTTPException(400, "OTP expired")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"is_verified": True, "trust_score": 60},
            "$unset": {"otp": ""}
        },
    )

    token = make_token(str(user["_id"]))

    return {
        "success": True,
        "token": token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"]
        },
    }

# ── Login ─────────────────────────────────────────────────────
@router.post("/login")
async def login(body: UserLogin, request: Request):
    db = get_db(request)
    user = await db.users.find_one({"email": body.email})

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(401, "Invalid credentials")

    # 🔥 CASE 1: USER NOT VERIFIED → SEND OTP
    if not user.get("is_verified"):
        otp = str(random.randint(100000, 999999))

        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "otp": {
                        "code": otp,
                        "expires_at": datetime.utcnow() + timedelta(minutes=10)
                    }
                }
            },
        )

        send_otp_email(user["email"], user["name"], otp)

        return {
            "success": True,
            "requires_otp": True,
            "user_id": str(user["_id"]),
            "message": "OTP sent again"
        }

    # 🔥 CASE 2: USER VERIFIED → NORMAL LOGIN
    token = make_token(str(user["_id"]))

    return {
        "success": True,
        "token": token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"]
        },
    }

# ── Me ────────────────────────────────────────────────────────
@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    u = current_user

    return UserOut(
        id=str(u["_id"]),
        name=u["name"],
        email=u["email"],
        branch=u.get("branch"),
        year=u.get("year"),
        stay_type=u.get("stay_type", "hostel"),
        hostel_block=u.get("hostel_block"),
        profile_photo=u.get("profile_photo"),
        trust_score=u.get("trust_score", 50),
        avg_rating=u.get("avg_rating", 0.0),
        is_verified=u.get("is_verified", False),
    )

# ── Update profile ────────────────────────────────────────────
@router.put("/me")
async def update_me(body: UserUpdate, request: Request, current_user: dict = Depends(get_current_user)):
    db = get_db(request)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    if updates:
        await db.users.update_one(
            {"_id": current_user["_id"]},
            {"$set": updates}
        )

    return {"success": True}
