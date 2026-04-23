from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from datetime import datetime
from bson import ObjectId
import jwt, os

from app.deps import get_db, get_current_user, JWT_SECRET

router = APIRouter()

# connection registry: {user_id: WebSocket}
_connections: dict[str, WebSocket] = {}


async def _ws_auth(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("id")
    except jwt.PyJWTError:
        return None


@router.websocket("/ws")
async def chat_ws(websocket: WebSocket):
    user_id = await _ws_auth(websocket)
    if not user_id:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    _connections[user_id] = websocket
    db = websocket.app.db

    try:
        while True:
            data = await websocket.receive_json()
            receiver_id = data.get("receiver_id")
            content = data.get("content", "").strip()
            booking_id = data.get("booking_id")

            if not receiver_id or not content:
                continue

            doc = {
                "sender_id": ObjectId(user_id),
                "receiver_id": ObjectId(receiver_id),
                "content": content,
                "booking_id": ObjectId(booking_id) if booking_id else None,
                "read_at": None,
                "created_at": datetime.utcnow(),
            }
            result = await db.messages.insert_one(doc)

            msg_out = {
                "id": str(result.inserted_id),
                "sender_id": user_id,
                "receiver_id": receiver_id,
                "content": content,
                "booking_id": booking_id,
                "created_at": doc["created_at"].isoformat(),
            }

            # Deliver to receiver if online
            if receiver_id in _connections:
                try:
                    await _connections[receiver_id].send_json(msg_out)
                except Exception:
                    _connections.pop(receiver_id, None)

            # Echo back to sender
            await websocket.send_json(msg_out)

    except WebSocketDisconnect:
        _connections.pop(user_id, None)


# ── REST: conversation history ────────────────────────────────────────────────

@router.get("/messages/{other_user_id}")
async def get_conversation(
    other_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]
    other = ObjectId(other_user_id)

    msgs = await db.messages.find({
        "$or": [
            {"sender_id": me, "receiver_id": other},
            {"sender_id": other, "receiver_id": me},
        ]
    }).sort("created_at", 1).to_list(200)

    # Mark unread as read
    await db.messages.update_many(
        {"sender_id": other, "receiver_id": me, "read_at": None},
        {"$set": {"read_at": datetime.utcnow()}},
    )

    def fmt(m):
        return {
            "id": str(m["_id"]),
            "sender_id": str(m["sender_id"]),
            "receiver_id": str(m["receiver_id"]),
            "content": m["content"],
            "booking_id": str(m["booking_id"]) if m.get("booking_id") else None,
            "read_at": m["read_at"].isoformat() if m.get("read_at") else None,
            "created_at": m["created_at"].isoformat(),
        }

    return {"success": True, "messages": [fmt(m) for m in msgs]}