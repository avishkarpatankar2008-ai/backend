from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
import os
from dotenv import load_dotenv

# ── Import Routers ─────────────────────────────────────────────
from app.routes.auth import router as auth_router
from app.routes.items import router as items_router
from app.routes.bookings import router as bookings_router
from app.routes.chat import router as chat_router
from app.routes.lost_found import router as lost_found_router
# ── Load Env ──────────────────────────────────────────────────
load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
print("MONGO_URL FROM ENV:", MONGO_URL)
if not MONGO_URL:
    raise ValueError("MONGO_URL is not set in environment variables")
DB_NAME = os.getenv("DB_NAME", "campusorbit")

# ── Lifespan (DB connection) ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.mongodb_client = AsyncIOMotorClient(MONGO_URL)
    app.db = app.mongodb_client[DB_NAME]

    # Create indexes
    await app.db.users.create_index("email", unique=True)
    await app.db.items.create_index([("location", "2dsphere")])
    await app.db.items.create_index([
        ("title", "text"),
        ("description", "text"),
        ("tags", "text")
    ])

    yield

    app.mongodb_client.close()

# ── App Init ──────────────────────────────────────────────────
app = FastAPI(
    title="CampusOrbit API",
    version="1.0.0",
    lifespan=lifespan
)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CLIENT_URL", "https://v0-campusorbit-rental-platform-fv1rcclwn.vercel.app/")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(items_router, prefix="/api/items", tags=["items"])
app.include_router(bookings_router, prefix="/api/bookings", tags=["bookings"])
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])              # ✅ ADDED
app.include_router(lost_found_router, prefix="/api/lost-found", tags=["lost-found"])  # ✅ ADDED

# ── Health Check ──────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "platform": "CampusOrbit"}
