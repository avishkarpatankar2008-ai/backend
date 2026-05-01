from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
import os
from dotenv import load_dotenv

from app.routes.auth import router as auth_router
from app.routes.items import router as items_router
from app.routes.bookings import router as bookings_router
from app.routes.chat import router as chat_router
from app.routes.lost_found import router as lost_found_router

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("MONGO_URL is not set in environment variables")

DB_NAME = os.getenv("DB_NAME", "campusorbit")

_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if not _raw_origins or _raw_origins.strip() == "*":
    ALLOWED_ORIGINS = ["*"]
    ALLOW_CREDENTIALS = False
else:
    ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _raw_origins.split(",") if o.strip()]
    ALLOW_CREDENTIALS = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.mongodb_client = AsyncIOMotorClient(MONGO_URL)
    app.db = app.mongodb_client[DB_NAME]

    # ── Core indexes ──────────────────────────────────────────────────────────
    await app.db.users.create_index("email", unique=True)
    await app.db.items.create_index([("location", "2dsphere")], sparse=True)
    await app.db.items.create_index([
        ("title", "text"),
        ("description", "text"),
        ("tags", "text"),
    ])

    # ── Chat performance indexes ──────────────────────────────────────────────
    # Compound index for message history queries (sender<->receiver pairs)
    await app.db.messages.create_index(
        [("sender_id", 1), ("receiver_id", 1), ("created_at", -1)],
        name="msg_thread_idx",
    )
    # Index for unread count and mark-seen queries
    await app.db.messages.create_index(
        [("receiver_id", 1), ("read_at", 1)],
        name="msg_unread_idx",
    )
    # Index for conversation list aggregation (both directions)
    await app.db.messages.create_index(
        [("receiver_id", 1), ("sender_id", 1), ("created_at", -1)],
        name="msg_conv_idx",
    )

    # ── User search text index ────────────────────────────────────────────────
    # NOTE: MongoDB only allows one text index per collection.
    # If this fails (already exists with different fields), it's safe to ignore.
    try:
        await app.db.users.create_index(
            [("name", "text"), ("email", "text"), ("college", "text")],
            name="user_search_text",
        )
    except Exception:
        pass  # Index already exists

    yield
    app.mongodb_client.close()


app = FastAPI(
    title="CampusOrbit API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(auth_router,       prefix="/api/auth",       tags=["auth"])
app.include_router(items_router,      prefix="/api/items",      tags=["items"])
app.include_router(bookings_router,   prefix="/api/bookings",   tags=["bookings"])
app.include_router(chat_router,       prefix="/api/chat",       tags=["chat"])
app.include_router(lost_found_router, prefix="/api/lost-found", tags=["lost-found"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "platform": "CampusOrbit"}
