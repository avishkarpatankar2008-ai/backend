from fastapi import APIRouter, Request, HTTPException, Depends, Response
from datetime import datetime, timedelta
import secrets, os, re, asyncio
from bson import ObjectId
import bcrypt
import jwt

from app.models import UserRegister, UserLogin, OTPVerify, UserUpdate
from app.deps import get_current_user, get_db

router = APIRouter()

@router.options("/{rest_of_path:path}")
async def options_handler():
    return Response(status_code=200)

JWT_SECRET     = os.getenv("JWT_SECRET", "changeme_secret_32chars_minimum!!")
JWT_EXPIRE_DAYS = 30
COLLEGE_EMAIL_RE = re.compile(r"\.(edu|ac\.in)$")


# ── Auth helpers ──────────────────────────────────────────────

def make_token(user_id: str) -> str:
    return jwt.encode(
        {"id": user_id, "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)},
        JWT_SECRET, algorithm="HS256",
    )


async def _hash(pwd: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: bcrypt.hashpw(pwd.encode(), bcrypt.gensalt(10)).decode()
    )


async def _verify(pwd: str, hashed: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: bcrypt.checkpw(pwd.encode(), hashed.encode())
    )


def _user_out(u: dict) -> dict:
    return {
        "id":          str(u["_id"]),
        "name":        u["name"],
        "email":       u["email"],
        "college":     u.get("college") or u.get("branch"),
        "phone":       u.get("phone"),
        "avatar":      u.get("profile_photo") or u.get("avatar"),
        "is_verified": u.get("is_verified", False),
        "trust_score": u.get("trust_score", 50),
        "avg_rating":  u.get("avg_rating", 0.0),
    }


def _make_otp() -> tuple[str, datetime]:
    """Cryptographically secure 6-digit OTP, valid 10 minutes."""
    return str(secrets.randbelow(900000) + 100000), datetime.utcnow() + timedelta(minutes=10)


# ── Email — Gmail SMTP via App Password ──────────────────────
#
# Required env vars (set in Render → Environment):
#   EMAIL_USER  your Gmail address          e.g. yourname@gmail.com
#   EMAIL_PASS  Gmail App Password (16 chars, NO spaces, NO quotes)
#               Google Account → Security → 2-Step Verification → App Passwords
#
# Optional (defaults shown):
#   EMAIL_HOST  smtp.gmail.com
#   EMAIL_PORT  587
#
# How it works:
#   1. Tries STARTTLS on EMAIL_PORT (587) — standard Gmail SMTP
#   2. Falls back to SMTP_SSL on port 465 if port 587 is blocked
#   3. If neither EMAIL_USER nor EMAIL_PASS is set (local dev),
#      prints the OTP to Render logs so you can copy it manually.

def _smtp_creds() -> tuple[str, int, str, str]:
    host = os.getenv("EMAIL_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("EMAIL_PORT", "587").strip())
    # Strip quotes/spaces that are accidentally pasted from Google's App Password UI
    # Google shows: "abcd efgh ijkl mnop" — we need: "abcdefghijklmnop"
    user = os.getenv("EMAIL_USER", "").strip().strip('"').strip("'")
    pwd  = os.getenv("EMAIL_PASS", "").strip().strip('"').strip("'").replace(" ", "")
    return host, port, user, pwd


def _send_via_smtp_sync(to: str, subject: str, name: str, otp: str) -> None:
    """Blocking SMTP send — always called inside run_in_executor."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host, port, user, pwd = _smtp_creds()

    # Build a plain-text + HTML multipart email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"CampusOrbit <{user}>"
    msg["To"]      = to

    text_body = (
        f"Hi {name},\n\n"
        f"Your CampusOrbit verification code is: {otp}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"— The CampusOrbit Team"
    )
    html_body = (
        f'<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">'
        f'<h2 style="color:#4f46e5;margin-bottom:4px">CampusOrbit</h2>'
        f'<p style="color:#374151">Hi {name},</p>'
        f'<p style="color:#374151">Your verification code is:</p>'
        f'<div style="font-size:40px;font-weight:bold;letter-spacing:10px;color:#4f46e5;'
        f'padding:20px;background:#f5f3ff;border-radius:12px;text-align:center;margin:20px 0">'
        f'{otp}</div>'
        f'<p style="color:#6b7280;font-size:13px">Expires in 10 minutes.<br>'
        f'Ignore this email if you did not request it.</p>'
        f'</div>'
    )

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    last: Exception | None = None

    # Attempt 1 — STARTTLS on configured port (default 587)
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
        print(f"[EMAIL] Sent '{subject}' to {to} via {host}:{port} STARTTLS ✓")
        return
    except smtplib.SMTPAuthenticationError as e:
        # Wrong password — no point trying port 465
        raise RuntimeError(
            f"Gmail authentication failed for {user}.\n"
            f"  • EMAIL_PASS must be a 16-character App Password, NOT your Google login password.\n"
            f"  • Generate one at: Google Account → Security → 2-Step Verification → App Passwords\n"
            f"  • Paste it WITHOUT spaces and WITHOUT quotes into Render environment.\n"
            f"  • Raw error: {e}"
        ) from e
    except Exception as e:
        last = e
        print(f"[EMAIL] STARTTLS:{port} failed ({e}), retrying with SSL:465 …")

    # Attempt 2 — SMTP_SSL on port 465
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=15) as s:
            s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
        print(f"[EMAIL] Sent '{subject}' to {to} via {host}:465 SSL ✓")
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"Gmail authentication failed on port 465 too.\n"
            f"  • Check EMAIL_USER and EMAIL_PASS in Render environment.\n"
            f"  • Raw error: {e}"
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Gmail SMTP failed on both ports.\n"
            f"  • STARTTLS:{port} error: {last}\n"
            f"  • SSL:465 error: {e}\n"
            f"  • Check EMAIL_HOST / EMAIL_PORT env vars.\n"
            f"  • Ensure 2-Step Verification is ON and you're using an App Password."
        ) from e


async def _send_otp_email(
    to: str, name: str, otp: str,
    subject: str = "CampusOrbit — Verify your email",
) -> str | None:
    """
    Async wrapper around _send_via_smtp_sync.
    ALWAYS awaited directly in route handlers — never fire-and-forget.
    Returns None on success, error string on failure.
    """
    _, _, smtp_user, smtp_pwd = _smtp_creds()
    loop = asyncio.get_running_loop()

    if smtp_user and smtp_pwd:
        try:
            await loop.run_in_executor(None, _send_via_smtp_sync, to, subject, name, otp)
            return None
        except Exception as e:
            err = str(e)
            print(f"[EMAIL ERROR] {err}")
            return err

    # Dev / not configured — OTP visible in Render logs
    print(
        f"\n{'='*60}\n"
        f"[EMAIL NOT CONFIGURED]  OTP for {to}: {otp}\n"
        f"Set EMAIL_USER + EMAIL_PASS in Render → Environment to send real emails.\n"
        f"{'='*60}\n"
    )
    return None


# ── Diagnostic endpoint ───────────────────────────────────────

@router.post("/test-email")
async def test_email(request: Request):
    """
    POST /api/auth/test-email  {"to": "you@example.com"}
    Call this right after deploy to confirm email is working.
    Returns config details + any error. Remove once confirmed.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    to = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(400, "Provide 'to' email in body")

    _, _, smtp_user, smtp_pwd = _smtp_creds()
    raw_pass = os.getenv("EMAIL_PASS", "")

    config = {
        "mode":                   "smtp" if (smtp_user and smtp_pwd) else "NOT CONFIGURED — OTPs only in Render logs",
        "EMAIL_USER":             smtp_user or "NOT SET",
        "EMAIL_PASS":             f"set ({len(smtp_pwd)} chars after sanitize)" if smtp_pwd else "NOT SET",
        "EMAIL_PASS_raw_len":     len(raw_pass),
        "EMAIL_PASS_cleaned_len": len(smtp_pwd),
        "EMAIL_HOST":             os.getenv("EMAIL_HOST", "smtp.gmail.com"),
        "EMAIL_PORT":             os.getenv("EMAIL_PORT", "587"),
    }

    err = await _send_otp_email(to, "Test User", "123456", "CampusOrbit — Email Test")
    if err:
        return {"success": False, "config": config, "error": err}
    return {"success": True, "config": config, "message": f"Test email sent to {to}"}


# ── Register ──────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: UserRegister, request: Request):
    db = get_db(request)

    if not COLLEGE_EMAIL_RE.search(body.email):
        raise HTTPException(400, "Use your college email (.edu or .ac.in)")

    existing, hashed = await asyncio.gather(
        db.users.find_one({"email": body.email}, {"_id": 1}),
        _hash(body.password),
    )
    if existing:
        raise HTTPException(400, "Email already registered")

    otp, expires_at = _make_otp()
    doc = {
        "name":          body.name,
        "email":         body.email,
        "password":      hashed,
        "college":       body.college or body.branch,
        "branch":        body.branch or body.college,
        "phone":         body.phone,
        "year":          body.year,
        "stay_type":     body.stay_type,
        "hostel_block":  body.hostel_block,
        "profile_photo": None,
        "avatar":        None,
        "role":          "student",
        "is_verified":   False,
        "otp":           {"code": otp, "expires_at": expires_at},
        "trust_score":   50,
        "total_ratings": 0,
        "avg_rating":    0.0,
        "upi_id":        None,
        "location":      {"type": "Point", "coordinates": [0, 0]},
        "created_at":    datetime.utcnow(),
    }
    result = await db.users.insert_one(doc)

    # Await directly — errors are visible in response + logs
    email_err = await _send_otp_email(body.email, body.name, otp)

    resp: dict = {
        "success":      True,
        "user_id":      str(result.inserted_id),
        "requires_otp": True,
        "message":      "OTP sent to your college email",
    }
    if email_err:
        resp["email_error"] = email_err
        resp["message"] = (
            "Account created but email delivery failed — check email_error field and Render logs. "
            "You can still get your OTP from Render logs (search for 'OTP for')."
        )
    return resp


# ── Verify OTP ────────────────────────────────────────────────

@router.post("/verify-otp")
async def verify_otp(body: OTPVerify, request: Request):
    db = get_db(request)

    if body.user_id:
        try:
            oid = ObjectId(body.user_id)
        except Exception:
            raise HTTPException(400, "Invalid user_id")
        user = await db.users.find_one({"_id": oid})
    elif body.email:
        user = await db.users.find_one({"email": body.email})
    else:
        raise HTTPException(400, "Provide user_id or email")

    if not user:
        raise HTTPException(404, "User not found")

    otp_data = user.get("otp") or {}
    if not otp_data.get("code"):
        raise HTTPException(400, "No pending OTP. Please request a new one.")
    if otp_data["code"] != body.otp:
        raise HTTPException(400, "Invalid OTP")
    expires = otp_data.get("expires_at")
    if expires and expires < datetime.utcnow():
        raise HTTPException(400, "OTP expired. Please request a new one.")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"is_verified": True, "trust_score": 60}, "$unset": {"otp": ""}},
    )

    token   = make_token(str(user["_id"]))
    out     = _user_out(user)
    out["is_verified"] = True
    return {"success": True, "token": token, "access_token": token, "user": out}


# ── Resend OTP ────────────────────────────────────────────────

@router.post("/resend-otp")
async def resend_otp(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    db = get_db(request)

    uid = body.get("user_id"); email = body.get("email")
    if uid:
        try: user = await db.users.find_one({"_id": ObjectId(uid)})
        except Exception: raise HTTPException(400, "Invalid user_id")
    elif email:
        user = await db.users.find_one({"email": email})
    else:
        raise HTTPException(400, "Provide user_id or email")

    if not user: raise HTTPException(404, "User not found")
    if user.get("is_verified"): raise HTTPException(400, "Already verified")

    otp, expires_at = _make_otp()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
    )
    email_err = await _send_otp_email(user["email"], user["name"], otp)

    resp: dict = {"success": True, "message": "New OTP sent to your email"}
    if email_err:
        resp["email_error"] = email_err
        resp["message"] = "OTP regenerated but email failed — check Render logs."
    return resp


# ── Forgot Password — send OTP ────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email required")

    db   = get_db(request)
    user = await db.users.find_one({"email": email})
    if user:
        otp, expires_at = _make_otp()
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"reset_otp": {"code": otp, "expires_at": expires_at}}},
        )
        await _send_otp_email(user["email"], user["name"], otp,
                              subject="CampusOrbit — Password reset code")
    # Always same response to prevent user enumeration
    return {"success": True, "message": "If that email is registered, a reset code has been sent."}


# ── Forgot Password — verify OTP + set new password ──────────

@router.post("/reset-password")
async def reset_password(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    email    = (body.get("email") or "").strip().lower()
    otp_code = str(body.get("otp") or "").strip()
    new_pwd  = body.get("new_password") or ""
    if not email or not otp_code or not new_pwd:
        raise HTTPException(400, "email, otp, and new_password are required")
    if len(new_pwd) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    db   = get_db(request)
    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(400, "Invalid or expired reset code")

    rd = user.get("reset_otp") or {}
    if not rd.get("code"):
        raise HTTPException(400, "No reset was requested. Please start over.")
    if rd["code"] != otp_code:
        raise HTTPException(400, "Invalid reset code")
    if rd.get("expires_at") and rd["expires_at"] < datetime.utcnow():
        raise HTTPException(400, "Reset code expired. Please request a new one.")

    hashed = await _hash(new_pwd)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": hashed, "is_verified": True}, "$unset": {"reset_otp": ""}},
    )
    token = make_token(str(user["_id"]))
    out   = _user_out(user); out["is_verified"] = True
    return {"success": True, "token": token, "access_token": token,
            "user": out, "message": "Password reset successful"}


# ── Login ─────────────────────────────────────────────────────

@router.post("/login")
async def login(body: UserLogin, request: Request):
    db   = get_db(request)
    user = await db.users.find_one({"email": body.email})

    if not user:
        # Run a dummy verify to prevent timing-based user enumeration
        await _verify(body.password, "$2b$10$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        raise HTTPException(401, "Invalid credentials")

    if not await _verify(body.password, user["password"]):
        raise HTTPException(401, "Invalid credentials")

    if not user.get("is_verified"):
        otp, expires_at = _make_otp()
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
        )
        email_err = await _send_otp_email(user["email"], user["name"], otp)
        resp: dict = {
            "success": True, "requires_otp": True,
            "user_id": str(user["_id"]),
            "message": "Please verify your email. OTP sent.",
        }
        if email_err:
            resp["email_error"] = email_err
        return resp

    token = make_token(str(user["_id"]))
    return {"success": True, "token": token, "access_token": token, "user": _user_out(user)}


# ── Me ────────────────────────────────────────────────────────

@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {"user": _user_out(current_user)}


@router.put("/me")
async def update_me(
    body: UserUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "college" in updates:
        updates["branch"] = updates["college"]
    if updates:
        await db.users.update_one({"_id": current_user["_id"]}, {"$set": updates})
    return {"success": True}
