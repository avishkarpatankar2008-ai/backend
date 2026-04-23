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
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("id")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")

    user = await request.app.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(401, "User not found")
    return user