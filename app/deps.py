from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId
import jwt, os

JWT_SECRET = os.getenv("JWT_SECRET", "changeme_secret_32chars_minimum!!")
bearer = HTTPBearer()


def get_db(request: Request):
    return request.app.db


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    try:
        payload  = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user_id  = payload.get("id")
        if not user_id:
            raise HTTPException(401, "Malformed token: missing user id")
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token has expired. Please log in again.")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token. Please log in again.")

    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(401, "Malformed token: invalid user id")

    user = await request.app.db.users.find_one({"_id": oid})
    if not user:
        raise HTTPException(401, "User no longer exists")

    return user


def invalidate_user_cache(_token: str) -> None:
    """Placeholder — extend with Redis/cache busting if you add caching later."""
    pass
