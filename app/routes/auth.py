from fastapi import APIRouter, Request, HTTPException, Depends, Response
from datetime import datetime, timedelta
import secrets, os, re, asyncio, json
from bson import ObjectId
import bcrypt
import jwt
import urllib.request
import urllib.error

from app.models import UserRegister, UserLogin, OTPVerify, UserUpdate
from app.deps import get_current_user, get_db

router = APIRouter()

@router.options("/{rest_of_path:path}")
async def options_handler():
    return Response(status_code=200)

JWT_SECRET      = os.getenv("JWT_SECRET", "changeme_secret_32chars_minimum!!")
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


# ── Email — Brevo Transactional API (HTTPS, no SMTP ports needed) ──
#
# Required env vars (Render → Environment):
#   BREVO_API_KEY   your Brevo v3 API key
#   FROM_EMAIL      sender address verified in Brevo
#   FROM_NAME       sender display name  (default: CampusOrbit)
#
# Uses only stdlib urllib — zero extra dependencies, works on Render free tier.

def _brevo_creds() -> tuple[str, str, str]:
    api_key    = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("FROM_EMAIL", "").strip()
    from_name  = os.getenv("FROM_NAME", "CampusOrbit").strip()
    return api_key, from_email, from_name


def _build_otp_html(name: str, otp: str, subject_context: str = "verification") -> tuple[str, str]:
    """Returns (plain_text, html_body) for an OTP email."""
    plain = (
        f"Hi {name},\n\n"
        f"Your CampusOrbit {subject_context} code is: {otp}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"-- The CampusOrbit Team"
    )
    html = (
        f'<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">'
        f'<h2 style="color:#4f46e5;margin-bottom:4px">CampusOrbit</h2>'
        f'<p style="color:#374151">Hi {name},</p>'
        f'<p style="color:#374151">Your {subject_context} code is:</p>'
        f'<div style="font-size:40px;font-weight:bold;letter-spacing:10px;color:#4f46e5;'
        f'padding:20px;background:#f5f3ff;border-radius:12px;text-align:center;margin:20px 0">'
        f'{otp}</div>'
        f'<p style="color:#6b7280;font-size:13px">Expires in 10 minutes.<br>'
        f'Ignore this email if you did not request it.</p>'
        f'</div>'
    )
    return plain, html


def _send_via_brevo_sync(to_email: str, to_name: str, subject: str, plain: str, html: str) -> None:
    """
    Blocking Brevo API call — always called inside run_in_executor.
    Raises RuntimeError with a clear message on any failure.
    Uses only stdlib urllib — no extra pip package needed.
    """
    api_key, from_email, from_name = _brevo_creds()

    if not api_key:
        raise RuntimeError(
            "BREVO_API_KEY is not set. "
            "Add it in Render -> Environment -> BREVO_API_KEY."
        )
    if not from_email:
        raise RuntimeError(
            "FROM_EMAIL is not set. "
            "Add it in Render -> Environment -> FROM_EMAIL."
        )

    payload = {
        "sender":      {"name": from_name, "email": from_email},
        "to":          [{"email": to_email, "name": to_name}],
        "subject":     subject,
        "textContent": plain,
        "htmlContent": html,
    }

    data    = json.dumps(payload).encode("utf-8")
    url     = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "api-key":      api_key,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body   = resp.read().decode("utf-8", errors="replace")
        if status not in (200, 201):
            raise RuntimeError(
                f"Brevo API returned unexpected status {status}. "
                f"Response: {body[:300]}"
            )
        print(f"[EMAIL] Sent '{subject}' to {to_email} via Brevo (status={status}) OK")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            detail  = json.loads(raw)
            message = detail.get("message", raw[:300])
            code    = detail.get("code", "unknown")
        except Exception:
            message = raw[:300]
            code    = str(e.code)

        if e.code == 401:
            raise RuntimeError(
                f"Brevo authentication failed (401). "
                f"Check BREVO_API_KEY in Render environment. Detail: {message}"
            ) from e
        if e.code == 400:
            raise RuntimeError(
                f"Brevo rejected the request (400 {code}): {message}. "
                f"Verify FROM_EMAIL is a sender validated in your Brevo account."
            ) from e
        raise RuntimeError(f"Brevo API HTTP error {e.code}: {message}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Network error reaching Brevo API: {e.reason}. "
            f"Check Render outbound internet access."
        ) from e


async def _send_otp_email(
    to: str, name: str, otp: str,
    subject: str = "CampusOrbit -- Verify your email",
    subject_context: str = "verification",
) -> str | None:
    """
    Async wrapper. Always awaited in route handlers -- never fire-and-forget.
    Returns None on success, error string on failure.
    """
    api_key, from_email, _ = _brevo_creds()
    loop = asyncio.get_running_loop()

    if api_key and from_email:
        plain, html = _build_otp_html(name, otp, subject_context)
        try:
            await loop.run_in_executor(
                None, _send_via_brevo_sync, to, name, subject, plain, html
            )
            return None
        except Exception as e:
            err = str(e)
            print(f"[EMAIL ERROR] {err}")
            return err

    # Dev / not configured -- OTP visible in Render logs only
    print(
        f"\n{'='*60}\n"
        f"[EMAIL NOT CONFIGURED]  OTP for {to}: {otp}\n"
        f"Set BREVO_API_KEY + FROM_EMAIL in Render -> Environment.\n"
        f"{'='*60}\n"
    )
    return None


# ── Diagnostic endpoint ───────────────────────────────────────

@router.post("/test-email")
async def test_email(request: Request):
    """
    POST /api/auth/test-email  {"to": "you@example.com"}
    Call once after deploy to confirm Brevo is working. Remove when done.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    to = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(400, "Provide 'to' email in body")

    api_key, from_email, from_name = _brevo_creds()
    config = {
        "mode":        "brevo_api" if (api_key and from_email) else "NOT CONFIGURED",
        "BREVO_API_KEY": f"set ({len(api_key)} chars)" if api_key else "NOT SET",
        "FROM_EMAIL":  from_email or "NOT SET",
        "FROM_NAME":   from_name,
    }

    err = await _send_otp_email(to, "Test User", "123456",
                                "CampusOrbit -- Email Test", "test")
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

    # Send email BEFORE inserting -- if Brevo fails, no orphan account is created.
    email_err = await _send_otp_email(
        body.email, body.name, otp,
        subject="CampusOrbit -- Verify your email",
        subject_context="verification",
    )
    if email_err:
        raise HTTPException(
            502,
            f"Could not send verification email: {email_err}. "
            f"Account not created. Please try again or contact support."
        )

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

    return {
        "success":      True,
        "user_id":      str(result.inserted_id),
        "requires_otp": True,
        "message":      "OTP sent to your college email",
    }


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

    # Send first -- if it fails, old OTP stays valid in the DB
    email_err = await _send_otp_email(
        user["email"], user["name"], otp,
        subject="CampusOrbit -- Your new verification code",
        subject_context="verification",
    )
    if email_err:
        raise HTTPException(
            502,
            f"Could not send OTP email: {email_err}. Please try again shortly."
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
    )
    return {"success": True, "message": "New OTP sent to your email"}


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
        email_err = await _send_otp_email(
            user["email"], user["name"], otp,
            subject="CampusOrbit -- Password reset code",
            subject_context="password reset",
        )
        if not email_err:
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"reset_otp": {"code": otp, "expires_at": expires_at}}},
            )
        else:
            print(f"[FORGOT PWD] Email send failed for {email}: {email_err}")

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
        # Dummy verify to prevent timing-based user enumeration
        await _verify(body.password, "$2b$10$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        raise HTTPException(401, "Invalid credentials")

    if not await _verify(body.password, user["password"]):
        raise HTTPException(401, "Invalid credentials")

    if not user.get("is_verified"):
        otp, expires_at = _make_otp()
        email_err = await _send_otp_email(
            user["email"], user["name"], otp,
            subject="CampusOrbit -- Verify your email",
            subject_context="verification",
        )
        if email_err:
            raise HTTPException(
                502,
                f"Could not send verification email: {email_err}. Please try again."
            )
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"otp": {"code": otp, "expires_at": expires_at}}},
        )
        return {
            "success": True, "requires_otp": True,
            "user_id": str(user["_id"]),
            "message": "Please verify your email. OTP sent.",
        }

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
