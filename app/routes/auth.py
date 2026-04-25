from fastapi import APIRouter, Request, HTTPException, Depends, Response
from datetime import datetime, timedelta
import random, os, re, asyncio
from bson import ObjectId
import bcrypt
import jwt

from app.models import UserRegister, UserLogin, OTPVerify, UserOut, UserUpdate
from app.deps import get_current_user, get_db

# ── Router ────────────────────────────────────────────────────
router = APIRouter()

# Handle preflight (CORS)
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


def _build_user_out(u: dict) -> dict:
    """Build the user dict the frontend expects. Never includes password."""
    return {
        "id": str(u["_id"]),
        "name": u["name"],
        "email": u["email"],
        "college": u.get("college") or u.get("branch"),
        "phone": u.get("phone"),
        "avatar": u.get("profile_photo") or u.get("avatar"),
        "is_verified": u.get("is_verified", False),
        "trust_score": u.get("trust_score", 50),
        "avg_rating": u.get("avg_rating", 0.0),
    }


def _make_otp() -> tuple[str, datetime]:
    code = str(random.randint(100000, 999999))
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    return code, expires_at


async def _send_otp_email(email: str, name: str, otp: str):
    """Non-blocking email send — runs SMTP in a thread so the event loop is not blocked."""
    import smtplib
    from email.mime.text import MIMEText

    host = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    port = int(os.getenv("EMAIL_PORT", 587))
    user = os.getenv("EMAIL_USER", "")
    pwd  = os.getenv("EMAIL_PASS", "")

    if not user or not pwd:
        # Dev mode: print OTP to console instead of crashing
        print(f"[DEV] OTP for {email}: {otp}")
        return

    def _send():
        msg = MIMEText(
            f"Hi {name},\n\n"
            f"Your CampusOrbit verification code is: {otp}\n\n"
            f"This code expires in 10 minutes.\n\n"
            f"If you did not request this, please ignore this email."
        )
        msg["Subject"] = "CampusOrbit — Verify your email"
        msg["From"]    = user
        msg["To"]      = email
        try:
            with smtplib.SMTP(host, port) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        except Exception as e:
            print(f"[EMAIL ERROR] {e}")

    # Run blocking SMTP in a thread pool so we don't block the async event loop
    await asyncio.get_event_loop().run_in_executor(None, _send)


# ── Register ──────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: UserRegister, request: Request):
    db = get_db(request)

    if not COLLEGE_EMAIL_RE.search(body.email):
        raise HTTPException(400, "Use your college email (.edu or .ac.in)")

    if await db.users.find_one({"email": body.email}):
        raise HTTPException(400, "Email already registered")

    otp, expires_at = _make_otp()

    user_doc = {
        "name":         body.name,
        "email":        body.email,
        "password":     hash_password(body.password),
        "college":      body.college or body.branch,
        "branch":       body.branch or body.college,
        "phone":        body.phone,
        "year":         body.year,
        "stay_type":    body.stay_type,
        "hostel_block": body.hostel_block,
        "profile_photo": None,
        "avatar":       None,
        "role":         "student",
        "is_verified":  False,
        "otp": {
            "code":       otp,
            "expires_at": expires_at,
        },
        "trust_score":    50,
        "total_ratings":  0,
        "avg_rating":     0.0,
        "upi_id":         None,
        "location":       {"type": "Point", "coordinates": [0, 0]},
        "created_at":     datetime.utcnow(),
    }

    result = await db.users.insert_one(user_doc)
    await _send_otp_email(body.email, body.name, otp)

    return {
        "success":     True,
        "user_id":     str(result.inserted_id),
        "requires_otp": True,
        "message":     "OTP sent to your college email",
    }


# ── Verify OTP ────────────────────────────────────────────────

@router.post("/verify-otp")
async def verify_otp(body: OTPVerify, request: Request):
    db = get_db(request)

    # Accept user_id OR email — whichever the frontend sends
    if body.user_id:
        try:
            oid = ObjectId(body.user_id)
        except Exception:
            raise HTTPException(400, "Invalid user_id format")
        user = await db.users.find_one({"_id": oid})
    elif body.email:
        user = await db.users.find_one({"email": body.email})
    else:
        raise HTTPException(400, "Provide either user_id or email")

    if not user:
        raise HTTPException(404, "User not found")

    otp_data = user.get("otp") or {}

    if not otp_data.get("code"):
        raise HTTPException(400, "No pending OTP for this user. Request a new one.")

    if otp_data.get("code") != body.otp:
        raise HTTPException(400, "Invalid OTP")

    expires_at = otp_data.get("expires_at")
    if expires_at and expires_at < datetime.utcnow():
        raise HTTPException(400, "OTP has expired. Please request a new one.")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set":   {"is_verified": True, "trust_score": 60},
            "$unset": {"otp": ""},
        },
    )

    token    = make_token(str(user["_id"]))
    user_out = _build_user_out(user)
    # Patch is_verified to True in the returned object (the DB update happened above
    # but `user` still holds the old snapshot)
    user_out["is_verified"] = True

    return {
        "success":      True,
        "token":        token,
        "access_token": token,   # both names so either frontend version works
        "user":         user_out,
    }


# ── Resend OTP ────────────────────────────────────────────────
# ADDED: previously missing endpoint — the frontend's "Resend OTP" button calls this.

@router.post("/resend-otp")
async def resend_otp(request: Request):
    """
    Accepts JSON body: { "user_id": "..." } OR { "email": "..." }
    Generates a fresh OTP and (re-)sends the email.
    """
    import json
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    db = get_db(request)

    user_id = body.get("user_id")
    email   = body.get("email")

    if user_id:
        try:
            oid = ObjectId(user_id)
        except Exception:
            raise HTTPException(400, "Invalid user_id format")
        user = await db.users.find_one({"_id": oid})
    elif email:
        user = await db.users.find_one({"email": email})
    else:
        raise HTTPException(400, "Provide either user_id or email")

    if not user:
        raise HTTPException(404, "User not found")

    if user.get("is_verified"):
        raise HTTPException(400, "User is already verified")

    otp, expires_at = _make_otp()

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
    )

    await _send_otp_email(user["email"], user["name"], otp)

    return {"success": True, "message": "A new OTP has been sent to your email"}


# ── Login ─────────────────────────────────────────────────────

@router.post("/login")
async def login(body: UserLogin, request: Request):
    db = get_db(request)
    user = await db.users.find_one({"email": body.email})

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(401, "Invalid credentials")

    # Case 1: user is NOT verified → generate new OTP and ask them to verify
    if not user.get("is_verified"):
        otp, expires_at = _make_otp()

        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
        )

        await _send_otp_email(user["email"], user["name"], otp)

        return {
            "success":     True,
            "requires_otp": True,
            "user_id":     str(user["_id"]),
            "message":     "Please verify your email. OTP sent.",
        }

    # Case 2: user IS verified → return token + user
    token    = make_token(str(user["_id"]))
    user_out = _build_user_out(user)

    return {
        "success":      True,
        "token":        token,
        "access_token": token,
        "user":         user_out,
    }


# ── Me ────────────────────────────────────────────────────────

@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    user_out = _build_user_out(current_user)
    return {"user": user_out}


# ── Update profile ────────────────────────────────────────────

@router.put("/me")
async def update_me(
    body: UserUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    # Keep college/branch in sync
    if "college" in updates:
        updates["branch"] = updates["college"]

    if updates:
        await db.users.update_one(
            {"_id": current_user["_id"]},
            {"$set": updates},
        )

    return {"success": True}
