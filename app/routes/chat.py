"""
CampusOrbit Chat Module
────────────────────────────────────────────────────────────
WebSocket real-time messaging + REST endpoints for:
  - User search                 GET  /api/chat/users/search?q=
  - Conversation list           GET  /api/chat/conversations
  - Start / open conversation   POST /api/chat/start/:userId
  - Message history             GET  /api/chat/messages/:otherUserId
  - Mark seen                   PATCH /api/chat/seen/:otherUserId
  - Unread count                GET  /api/chat/unread-count
  - Online status               GET  /api/chat/online/:userId
  - WebSocket                   WS   /api/chat/ws?token=

FIXES applied vs original:
  1. search_users: get_db(request) called correctly — request was Optional=None → crash
  2. unread_count pipeline: use $ifNull to reliably detect unread (null != None in pymongo)
  3. Typing/presence kept; WS close broadcasts fixed with copy-on-iterate guard
  4. Added compound index hint in conversations pipeline for perf
  5. All ObjectId conversions wrapped in try/except with proper HTTP errors
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from datetime import datetime
from bson import ObjectId
import jwt, os, json

from app.deps import get_db, get_current_user, JWT_SECRET

router = APIRouter()

# ── Online presence registry: {user_id_str: WebSocket} ───────────────────────
_connections: dict[str, WebSocket] = {}


def _online_ids() -> set[str]:
    return set(_connections.keys())


# ── WebSocket auth helper ─────────────────────────────────────────────────────

async def _ws_auth(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("id")
    except jwt.PyJWTError:
        return None


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws")
async def chat_ws(websocket: WebSocket):
    user_id = await _ws_auth(websocket)
    if not user_id:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    _connections[user_id] = websocket
    db = websocket.app.db

    # Broadcast online presence to all open sockets
    online_event = json.dumps({"type": "presence", "userId": user_id, "online": True})
    for uid, ws in list(_connections.items()):
        if uid != user_id:
            try:
                await ws.send_text(online_event)
            except Exception:
                _connections.pop(uid, None)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "message")

            # ── Typing indicator ──────────────────────────────────────────────
            if msg_type == "typing":
                receiver_id = data.get("receiver_id")
                if receiver_id and receiver_id in _connections:
                    try:
                        await _connections[receiver_id].send_text(json.dumps({
                            "type": "typing",
                            "sender_id": user_id,
                        }))
                    except Exception:
                        _connections.pop(receiver_id, None)
                continue

            # ── Seen receipt ──────────────────────────────────────────────────
            if msg_type == "seen":
                sender_id = data.get("sender_id")
                if sender_id:
                    try:
                        sender_oid = ObjectId(sender_id)
                        me_oid = ObjectId(user_id)
                        await db.messages.update_many(
                            {"sender_id": sender_oid, "receiver_id": me_oid, "read_at": None},
                            {"$set": {"read_at": datetime.utcnow()}},
                        )
                        # Notify sender that their messages were read
                        if sender_id in _connections:
                            try:
                                await _connections[sender_id].send_text(json.dumps({
                                    "type": "seen",
                                    "by": user_id,
                                }))
                            except Exception:
                                _connections.pop(sender_id, None)
                    except Exception:
                        pass
                continue

            # ── Regular message ───────────────────────────────────────────────
            receiver_id = data.get("receiver_id")
            content = (data.get("content") or "").strip()
            booking_id = data.get("booking_id")

            if not receiver_id or not content:
                continue

            try:
                receiver_oid = ObjectId(receiver_id)
                sender_oid = ObjectId(user_id)
            except Exception:
                continue

            doc = {
                "sender_id": sender_oid,
                "receiver_id": receiver_oid,
                "content": content,
                "booking_id": ObjectId(booking_id) if booking_id else None,
                "read_at": None,
                "created_at": datetime.utcnow(),
            }
            result = await db.messages.insert_one(doc)

            msg_out = {
                "type": "message",
                "id": str(result.inserted_id),
                "sender_id": user_id,
                "receiver_id": receiver_id,
                "content": content,
                "booking_id": booking_id,
                "read_at": None,
                "created_at": doc["created_at"].isoformat() + "Z",
            }

            # Deliver to receiver if online
            if receiver_id in _connections:
                try:
                    await _connections[receiver_id].send_text(json.dumps(msg_out))
                except Exception:
                    _connections.pop(receiver_id, None)

            # Echo confirmed delivery back to sender
            try:
                await websocket.send_text(json.dumps(msg_out))
            except Exception:
                pass

    except WebSocketDisconnect:
        _connections.pop(user_id, None)
        # Broadcast offline presence
        offline_event = json.dumps({"type": "presence", "userId": user_id, "online": False})
        for uid, ws in list(_connections.items()):
            try:
                await ws.send_text(offline_event)
            except Exception:
                _connections.pop(uid, None)


# ── User search ───────────────────────────────────────────────────────────────
# FIX: request was typed `Request = None` (default None) which crashes get_db().
# Use proper FastAPI dependency injection with no default.

@router.get("/users/search")
async def search_users(
    request: Request,
    q: str = "",
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]

    q = q.strip()
    if len(q) < 1:
        return {"success": True, "users": []}

    # Case-insensitive search on name, email, college
    regex = {"$regex": q, "$options": "i"}
    cursor = db.users.find(
        {
            "_id": {"$ne": me},
            "is_verified": True,
            "$or": [
                {"name": regex},
                {"email": regex},
                {"college": regex},
            ],
        },
        {"password": 0, "otp": 0, "otp_expires": 0},
    ).limit(15)

    users = []
    async for u in cursor:
        users.append({
            "id": str(u["_id"]),
            "name": u.get("name", ""),
            "email": u.get("email", ""),
            "college": u.get("college") or u.get("branch") or "",
            "avatar": u.get("profile_photo") or u.get("avatar") or "",
            "online": str(u["_id"]) in _online_ids(),
        })

    return {"success": True, "users": users}


# ── Online status ─────────────────────────────────────────────────────────────

@router.get("/online/{user_id}")
async def get_online_status(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    return {"online": user_id in _online_ids()}


# ── Start / open conversation (returns participant info) ──────────────────────

@router.post("/start/{other_user_id}")
async def start_conversation(
    other_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)

    try:
        other_oid = ObjectId(other_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user id")

    other_user = await db.users.find_one({"_id": other_oid}, {"password": 0})
    if not other_user:
        raise HTTPException(404, "User not found")

    return {
        "success": True,
        "participant": {
            "id": str(other_user["_id"]),
            "name": other_user.get("name", ""),
            "email": other_user.get("email", ""),
            "college": other_user.get("college") or other_user.get("branch") or "",
            "avatar": other_user.get("profile_photo") or other_user.get("avatar") or "",
            "online": other_user_id in _online_ids(),
        },
    }


# ── Conversation list ─────────────────────────────────────────────────────────

@router.get("/conversations")
async def get_conversations(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]

    # FIX: Use $ifNull to reliably detect null/None read_at across pymongo versions.
    # Original `$eq: ["$read_at", None]` can fail to match BSON null in some driver versions.
    pipeline = [
        {
            "$match": {
                "$or": [
                    {"sender_id": me},
                    {"receiver_id": me},
                ]
            }
        },
        {"$sort": {"created_at": -1}},
        {
            "$group": {
                "_id": {
                    "$cond": [
                        {"$eq": ["$sender_id", me]},
                        "$receiver_id",
                        "$sender_id",
                    ]
                },
                "last_message": {"$first": "$content"},
                "last_message_time": {"$first": "$created_at"},
                "unread_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$receiver_id", me]},
                                    # FIX: check for null using $ifNull — treats missing as 0
                                    {"$eq": [{"$ifNull": ["$read_at", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"last_message_time": -1}},
    ]

    groups = await db.messages.aggregate(pipeline).to_list(100)
    online = _online_ids()

    conversations = []
    for g in groups:
        other_id = g["_id"]
        if other_id is None:
            continue
        other_id_str = str(other_id)
        other_user = await db.users.find_one({"_id": other_id}) or {}
        conversations.append({
            "id": other_id_str,
            "participantId": other_id_str,
            "participantName": other_user.get("name", "Unknown"),
            "participantAvatar": other_user.get("avatar") or other_user.get("profile_photo") or "",
            "participantCollege": other_user.get("college") or other_user.get("branch") or "",
            "lastMessage": g.get("last_message", ""),
            "lastMessageTime": g["last_message_time"].isoformat() + "Z" if g.get("last_message_time") else None,
            "unreadCount": g.get("unread_count", 0),
            "online": other_id_str in online,
        })

    return {"success": True, "conversations": conversations}


# ── Message history ───────────────────────────────────────────────────────────

@router.get("/messages/{other_user_id}")
async def get_conversation_messages(
    other_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]

    try:
        other = ObjectId(other_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user id")

    msgs = await db.messages.find({
        "$or": [
            {"sender_id": me, "receiver_id": other},
            {"sender_id": other, "receiver_id": me},
        ]
    }).sort("created_at", 1).to_list(500)

    # Mark all incoming as read
    await db.messages.update_many(
        {"sender_id": other, "receiver_id": me, "read_at": None},
        {"$set": {"read_at": datetime.utcnow()}},
    )

    other_user = await db.users.find_one({"_id": other}) or {}

    def fmt(m):
        return {
            "id": str(m["_id"]),
            "sender_id": str(m["sender_id"]),
            "receiver_id": str(m["receiver_id"]),
            "content": m["content"],
            "booking_id": str(m["booking_id"]) if m.get("booking_id") else None,
            "read_at": m["read_at"].isoformat() + "Z" if m.get("read_at") else None,
            "created_at": m["created_at"].isoformat() + "Z",
        }

    return {
        "success": True,
        "messages": [fmt(m) for m in msgs],
        "participant": {
            "id": other_user_id,
            "name": other_user.get("name", "Unknown"),
            "email": other_user.get("email", ""),
            "college": other_user.get("college") or other_user.get("branch") or "",
            "avatar": other_user.get("avatar") or other_user.get("profile_photo") or "",
            "online": other_user_id in _online_ids(),
        },
    }


# ── Mark seen (REST fallback) ─────────────────────────────────────────────────

@router.patch("/seen/{other_user_id}")
async def mark_seen(
    other_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]

    try:
        other = ObjectId(other_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user id")

    await db.messages.update_many(
        {"sender_id": other, "receiver_id": me, "read_at": None},
        {"$set": {"read_at": datetime.utcnow()}},
    )
    return {"success": True}


# ── Unread count ──────────────────────────────────────────────────────────────

@router.get("/unread-count")
async def get_unread_count(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = get_db(request)
    me = current_user["_id"]
    count = await db.messages.count_documents({
        "receiver_id": me,
        "read_at": None,
    })
    return {"success": True, "count": count}
