from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ── Shared ────────────────────────────────────────────────────────────────────

class PyObjectId(str):
    pass


# ── User ──────────────────────────────────────────────────────────────────────

class StayType(str, Enum):
    hostel = "hostel"
    pg = "pg"
    day_scholar = "day_scholar"


class UserRegister(BaseModel):
    name: str
    email: EmailStr
    password: str
    # Frontend sends "college" and "phone" — accept both naming conventions
    college: Optional[str] = None
    phone: Optional[str] = None
    # Legacy / native-app fields (optional, ignored if not sent)
    branch: Optional[str] = None
    year: Optional[int] = None
    stay_type: StayType = StayType.hostel
    hostel_block: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class OTPVerify(BaseModel):
    # Frontend may send user_id OR email — both are accepted
    user_id: Optional[str] = None
    email: Optional[EmailStr] = None
    otp: str


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    # Frontend-facing fields
    college: Optional[str] = None
    phone: Optional[str] = None
    avatar: Optional[str] = None
    # Extended fields
    branch: Optional[str] = None
    year: Optional[int] = None
    stay_type: str = "hostel"
    hostel_block: Optional[str] = None
    profile_photo: Optional[str] = None
    trust_score: float = 50.0
    avg_rating: float = 0.0
    is_verified: bool = False


class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    college: Optional[str] = None
    hostel_block: Optional[str] = None
    profile_photo: Optional[str] = None
    avatar: Optional[str] = None
    upi_id: Optional[str] = None


# ── Item ──────────────────────────────────────────────────────────────────────

class ItemCategory(str, Enum):
    books = "books"
    electronics = "electronics"
    lab_equipment = "lab_equipment"
    instruments = "instruments"
    sports = "sports"
    stationery = "stationery"
    other = "other"


class ItemCondition(str, Enum):
    like_new = "like_new"
    good = "good"
    fair = "fair"
    acceptable = "acceptable"


class ItemCreate(BaseModel):
    title: str
    description: Optional[str] = None
    category: ItemCategory
    condition: ItemCondition
    price_per_day: float = Field(gt=0)
    security_deposit: float = 0
    max_rental_days: int = 30
    location_name: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    instant_booking: bool = False
    barter_ok: bool = False


class ItemUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price_per_day: Optional[float] = None
    max_rental_days: Optional[int] = None
    is_available: Optional[bool] = None
    instant_booking: Optional[bool] = None
    barter_ok: Optional[bool] = None


class ItemOut(BaseModel):
    id: str
    owner_id: str
    owner_name: str
    owner_avg_rating: float
    owner_hostel_block: Optional[str]
    owner_trust_score: float
    title: str
    description: Optional[str]
    category: str
    condition: str
    images: List[str]
    price_per_day: float
    security_deposit: float
    max_rental_days: int
    is_available: bool
    instant_booking: bool
    barter_ok: bool
    location_name: str
    avg_rating: float
    views: int
    tags: List[str]
    created_at: datetime


# ── Booking ───────────────────────────────────────────────────────────────────

class BookingStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    active = "active"
    returned = "returned"
    cancelled = "cancelled"


class BookingCreate(BaseModel):
    item_id: str
    start_date: datetime
    end_date: datetime


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class BookingOut(BaseModel):
    id: str
    item_id: str
    item_title: str
    renter_id: str
    renter_name: str
    owner_id: str
    owner_name: str
    start_date: datetime
    end_date: datetime
    total_days: int
    total_cost: float
    status: str
    created_at: datetime


# ── Lost & Found ──────────────────────────────────────────────────────────────

class LostFoundType(str, Enum):
    lost = "lost"
    found = "found"


class LostFoundCreate(BaseModel):
    type: LostFoundType
    title: str
    description: str
    category: Optional[str] = None
    location: str
    date_lost_found: Optional[datetime] = None
    contact_email: Optional[str] = None
    reward: float = 0


class LostFoundOut(BaseModel):
    id: str
    reported_by_id: str
    reported_by_name: str
    type: str
    title: str
    description: str
    category: Optional[str]
    images: List[str]
    location: str
    status: str
    reward: float
    created_at: datetime


# ── Message ───────────────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    receiver_id: str
    content: str
    booking_id: Optional[str] = None


class MessageOut(BaseModel):
    id: str
    sender_id: str
    receiver_id: str
    content: str
    booking_id: Optional[str]
    read_at: Optional[datetime]
    created_at: datetime
