from fastapi import FastAPI, APIRouter, HTTPException, Header
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os, logging, uuid, httpx
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'lumina')]

app = FastAPI()
api_router = APIRouter(prefix="/api")
EMERGENT_SESSION_DATA_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"

def now_utc(): return datetime.now(timezone.utc)
def week_id_for(dt): y,w,_=dt.isocalendar(); return f"{y}-W{w:02d}"

async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    session = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not session: raise HTTPException(status_code=401, detail="Invalid session")
    user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
    if not user: raise HTTPException(status_code=401, detail="User not found")
    return user

class SessionCreate(BaseModel): session_token: str
class PhotoCreate(BaseModel): image_base64: str; caption: Optional[str] = ""
class VoteCreate(BaseModel): photo_id: str

@api_router.get("/")
async def root(): return {"message": "Lumina API", "week": week_id_for(now_utc())}

@api_router.post("/auth/session")
async def create_session(body: SessionCreate):
    async with httpx.AsyncClient(timeout=15.0) as hc:
        r = await hc.get(EMERGENT_SESSION_DATA_URL, headers={"X-Session-ID": body.session_token})
    if r.status_code != 200: raise HTTPException(status_code=401, detail="Invalid session")
    data = r.json()
    email = data["email"]; name = data.get("name") or email.split("@")[0]
    picture = data.get("picture") or ""; session_token = data["session_token"]
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        user_id = existing["user_id"]
        await db.users.update_one({"user_id": user_id}, {"$set": {"name": name, "picture": picture, "last_login": now_utc()}})
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        await db.users.insert_one({"user_id": user_id, "email": email, "name": name, "picture": picture, "created_at": now_utc(), "last_login": now_utc()})
    await db.user_sessions.insert_one({"session_token": session_token, "user_id": user_id, "expires_at": now_utc() + timedelta(days=7), "created_at": now_utc()})
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return {"session_token": session_token, "user": user}

@api_router.get("/auth/me")
async def auth_me(authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization); return {"user": user}

@api_router.post("/auth/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        await db.user_sessions.delete_one({"session_token": authorization.split(" ", 1)[1].strip()})
    return {"ok": True}

@api_router.post("/photos")
async def upload_photo(body: PhotoCreate, authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization)
    wid = week_id_for(now_utc()); photo_id = f"photo_{uuid.uuid4().hex[:12]}"
    await db.photos.insert_one({"photo_id": photo_id, "user_id": user["user_id"], "week_id": wid, "image_base64": body.image_base64, "caption": body.caption or "", "created_at": now_utc()})
    return {"photo_id": photo_id, "week_id": wid}

@api_router.get("/photos/current")
async def list_current_photos(authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization); wid = week_id_for(now_utc())
    photos = await db.photos.find({"week_id": wid}, {"_id": 0, "user_id": 0}).to_list(500)
    mine_ids = {p["photo_id"] for p in await db.photos.find({"week_id": wid, "user_id": user["user_id"]}, {"_id": 0, "photo_id": 1}).to_list(500)}
    vote = await db.votes.find_one({"user_id": user["user_id"], "week_id": wid}, {"_id": 0})
    return {"week_id": wid, "photos": [{"photo_id": p["photo_id"], "image_base64": p["image_base64"], "caption": p.get("caption", ""), "is_mine": p["photo_id"] in mine_ids} for p in photos], "voted_photo_id": vote["photo_id"] if vote else None}

@api_router.get("/photos/{photo_id}")
async def get_photo(photo_id: str, authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization)
    p = await db.photos.find_one({"photo_id": photo_id}, {"_id": 0})
    if not p: raise HTTPException(status_code=404, detail="Photo not found")
    wid = week_id_for(now_utc()); is_current = p["week_id"] == wid; is_mine = p["user_id"] == user["user_id"]
    vote = await db.votes.find_one({"user_id": user["user_id"], "week_id": p["week_id"]}, {"_id": 0})
    result = {"photo_id": p["photo_id"], "image_base64": p["image_base64"], "caption": p.get("caption", ""), "week_id": p["week_id"], "is_mine": is_mine, "is_current_week": is_current, "user_voted_photo_id": vote["photo_id"] if vote else None}
    if not is_current or is_mine:
        author = await db.users.find_one({"user_id": p["user_id"]}, {"_id": 0, "name": 1, "picture": 1})
        if author: result["author_name"] = author.get("name"); result["author_picture"] = author.get("picture")
        result["votes_count"] = await db.votes.count_documents({"photo_id": photo_id})
    return result

@api_router.put("/photos/{photo_id}")
async def replace_photo(photo_id: str, body: PhotoCreate, authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization)
    # Find the existing photo
    p = await db.photos.find_one({"photo_id": photo_id}, {"_id": 0})
    if not p: raise HTTPException(status_code=404, detail="Photo not found")
    # Only the owner can replace
    if p["user_id"] != user["user_id"]: raise HTTPException(status_code=403, detail="Not your photo")
    # Can only replace during the current week
    wid = week_id_for(now_utc())
    if p["week_id"] != wid: raise HTTPException(status_code=400, detail="Cannot replace a photo from a past week")
    # Update only image and caption — preserve everything else
    await db.photos.update_one(
        {"photo_id": photo_id},
        {"$set": {
            "image_base64": body.image_base64,
            "caption": body.caption or "",
            "updated_at": now_utc()
        }}
    )
    updated = await db.photos.find_one({"photo_id": photo_id}, {"_id": 0})
    return {
        "photo_id": updated["photo_id"],
        "week_id": updated["week_id"],
        "caption": updated.get("caption", ""),
        "updated_at": updated.get("updated_at").isoformat() if updated.get("updated_at") else None
    }

@api_router.post("/votes")
async def cast_vote(body: VoteCreate, authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization)
    photo = await db.photos.find_one({"photo_id": body.photo_id}, {"_id": 0})
    if not photo: raise HTTPException(status_code=404, detail="Photo not found")
    wid = week_id_for(now_utc())
    if photo["week_id"] != wid: raise HTTPException(status_code=400, detail="Voting closed")
    if photo["user_id"] == user["user_id"]: raise HTTPException(status_code=400, detail="Cannot vote for your own photo")
    if await db.votes.find_one({"user_id": user["user_id"], "week_id": wid}): raise HTTPException(status_code=400, detail="Already voted this week")
    await db.votes.insert_one({"vote_id": f"vote_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "photo_id": body.photo_id, "week_id": wid, "created_at": now_utc()})
    return {"ok": True}

@api_router.get("/results/weeks")
async def list_result_weeks(authorization: Optional[str] = Header(None)):
    await get_current_user(authorization); current = week_id_for(now_utc())
    weeks = await db.photos.distinct("week_id")
    return {"current_week": current, "past_weeks": sorted([w for w in weeks if w != current], reverse=True)}

@api_router.get("/results/week/{week_id}")
async def week_results(week_id: str, authorization: Optional[str] = Header(None)):
    await get_current_user(authorization); current = week_id_for(now_utc())
    photos = await db.photos.find({"week_id": week_id}, {"_id": 0}).to_list(500)
    items = []
    for p in photos:
        votes_count = await db.votes.count_documents({"photo_id": p["photo_id"]})
        author = await db.users.find_one({"user_id": p["user_id"]}, {"_id": 0, "name": 1, "picture": 1})
        items.append({"photo_id": p["photo_id"], "image_base64": p["image_base64"], "caption": p.get("caption", ""), "votes_count": votes_count, "author_name": author.get("name") if author else "Unknown", "author_picture": author.get("picture", "") if author else ""})
    items.sort(key=lambda x: x["votes_count"], reverse=True)
    return {"week_id": week_id, "is_current": week_id == current, "results": items}

@api_router.get("/leaderboard")
async def leaderboard(authorization: Optional[str] = Header(None)):
    await get_current_user(authorization)
    pipeline = [{"$lookup": {"from": "photos", "localField": "photo_id", "foreignField": "photo_id", "as": "ph"}}, {"$unwind": "$ph"}, {"$group": {"_id": "$ph.user_id", "votes": {"$sum": 1}}}, {"$sort": {"votes": -1}}, {"$limit": 10}]
    rows = await db.votes.aggregate(pipeline).to_list(10)
    result = []
    for r in rows:
        u = await db.users.find_one({"user_id": r["_id"]}, {"_id": 0, "name": 1, "picture": 1})
        result.append({"user_id": r["_id"], "name": u.get("name") if u else "Unknown", "picture": u.get("picture", "") if u else "", "votes": r["votes"]})
    return {"leaderboard": result}

@api_router.get("/me/photos")
async def my_photos(authorization: Optional[str] = Header(None)):
    user = await get_current_user(authorization)
    photos = await db.photos.find({"user_id": user["user_id"]}, {"_id": 0}).sort("created_at", -1).to_list(500)
    items = [{"photo_id": p["photo_id"], "image_base64": p["image_base64"], "caption": p.get("caption", ""), "week_id": p["week_id"], "votes_count": await db.votes.count_documents({"photo_id": p["photo_id"]})} for p in photos]
    return {"photos": items, "submissions": len(items), "wins": 0}

@app.on_event("startup")
async def on_startup():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.user_sessions.create_index("session_token", unique=True)
    await db.photos.create_index("photo_id", unique=True)
    await db.votes.create_index([("user_id", 1), ("week_id", 1)], unique=True)

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO)

@app.on_event("shutdown")
async def shutdown_db_client(): client.close()
