import os
import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

app = FastAPI(title="Server Planner V1")
DISCORD_API = "https://discord.com/api/v10"
DB_PATH = "analytics.db"

ADMIN_USER = os.environ.get("ADMIN_USER", "becquerel")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "becquerel@708090")
ENV_REVIEW_WEBHOOK = os.environ.get("REVIEW_WEBHOOK_URL", "").strip()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            guild_id TEXT,
            guild_name TEXT,
            user_hash TEXT,
            details TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_value TEXT UNIQUE,
            reason TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    if ENV_REVIEW_WEBHOOK:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('review_webhook', ?)",
            (ENV_REVIEW_WEBHOOK,)
        )
    conn.commit()
    conn.close()


init_db()


def get_review_webhook() -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = 'review_webhook'").fetchone()
    conn.close()
    if row and row[0].strip():
        return row[0].strip()
    return ENV_REVIEW_WEBHOOK


def get_user_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()[:12] if token else "-"


def is_blacklisted(guild_id: str, user_hash: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT reason FROM blacklist WHERE target_value = ? OR target_value = ?",
        (guild_id.strip(), user_hash)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def log_event(event_type: str, guild_id: str = "-", guild_name: str = "-", token: str = "", details: str = ""):
    user_hash = get_user_hash(token)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (event_type, guild_id, guild_name, user_hash, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (event_type, guild_id, guild_name, user_hash, details, now)
    )
    conn.commit()
    conn.close()


def verify_admin(username: str, password: str):
    if username.strip() != ADMIN_USER or password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Invalid admin username or password.")


class AuthPayload(BaseModel):
    token: str
    guild_id: str


class ReviewPayload(BaseModel):
    stars: int
    display_name: str
    discord_username: str
    review_text: str
    guild_name: Optional[str] = "Not connected"


class AdminAuthPayload(BaseModel):
    username: str
    password: str


class AdminWebhookPayload(BaseModel):
    username: str
    password: str
    webhook_url: str


class AdminDeleteLogPayload(BaseModel):
    username: str
    password: str
    log_id: Optional[int] = None


class AdminBlacklistPayload(BaseModel):
    username: str
    password: str
    target_value: str
    reason: str = "Blacklisted by admin"


class ActionItem(BaseModel):
    id: str
    type: str
    target_id: Optional[str] = None
    data: Dict[str, Any] = {}


class SingleStepPayload(BaseModel):
    token: str
    guild_id: str
    guild_name: Optional[str] = "Unknown"
    action: ActionItem
    is_final: bool = False
    total_count: int = 1


def get_headers(token: str) -> Dict[str, str]:
    clean_token = token.strip()
    if not clean_token.startswith("Bot "):
        clean_token = f"Bot {clean_token}"
    return {
        "Authorization": clean_token,
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": "Server Planner V1 - Becquerel development"
    }


async def discord_request(session: aiohttp.ClientSession, method: str, url: str, headers: dict, json_data: Any = None):
    while True:
        async with session.request(method, url, headers=headers, json=json_data) as resp:
            if resp.status == 429:
                retry_after = float(resp.headers.get("Retry-After", 1.5))
                await asyncio.sleep(retry_after + 0.2)
                continue
            if resp.status == 204:
                return {"ok": True}
            data = await resp.json()
            if resp.status >= 400:
                raise HTTPException(status_code=resp.status, detail=data.get("message", "Discord API Error"))
            return data


@app.get("/")
async def serve_ui():
    return FileResponse("ui.html")


@app.post("/api/review")
async def submit_review(payload: ReviewPayload):
    webhook_url = get_review_webhook()
    if not webhook_url:
        raise HTTPException(status_code=503, detail="Review webhook is not configured yet. Set it in the Admin Panel.")

    stars = max(1, min(5, int(payload.stars)))
    star_visual = "★" * stars + "☆" * (5 - stars)
    display_name = payload.display_name.strip()[:60] or "Anonymous"
    discord_user = payload.discord_username.strip()[:60] or "Not provided"
    review_msg = payload.review_text.strip()[:1500]
    if not review_msg:
        raise HTTPException(status_code=400, detail="Please write a short review message.")

    embed_color = 0x22C55E if stars >= 4 else (0xF59E0B if stars == 3 else 0xEF4444)
    rating_label = {5: "Outstanding", 4: "Great", 3: "Good", 2: "Fair", 1: "Needs Work"}[stars]

    webhook_body = {
        "username": "Server Planner V1 • Reviews",
        "embeds": [
            {
                "author": {"name": f"New Review from {display_name}"},
                "title": f"{star_visual}  ({stars}/5 — {rating_label})",
                "description": f"> {review_msg}",
                "color": embed_color,
                "fields": [
                    {"name": "👤 Display Name", "value": f"**{display_name}**", "inline": True},
                    {"name": "💬 Discord Username", "value": f"`{discord_user}`", "inline": True},
                    {"name": "🏰 Connected Server", "value": payload.guild_name or "Not connected", "inline": True}
                ],
                "footer": {"text": "Server Planner V1 — Becquerel development"},
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json=webhook_body) as resp:
            if resp.status >= 400:
                raise HTTPException(status_code=502, detail="Discord rejected the webhook request. Verify the webhook URL in Admin.")

    log_event("USER_REVIEW", guild_name=payload.guild_name or "-", details=f"{stars}★ review by {discord_user}")
    return {"ok": True}


@app.post("/api/load")
async def load_guild(payload: AuthPayload):
    u_hash = get_user_hash(payload.token)
    blocked_reason = is_blacklisted(payload.guild_id, u_hash)
    if blocked_reason:
        raise HTTPException(status_code=403, detail=f"Access denied: {blocked_reason}")

    headers = get_headers(payload.token)
    async with aiohttp.ClientSession() as session:
        guild = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}", headers)
        channels = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/channels", headers)
        roles = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/roles", headers)
        emojis = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/emojis", headers)
        bot_user = await discord_request(session, "GET", f"{DISCORD_API}/users/@me", headers)
        bot_member = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/members/{bot_user['id']}", headers)

    channels.sort(key=lambda c: c.get("position", 0))
    roles.sort(key=lambda r: r.get("position", 0), reverse=True)

    bot_role_ids = set(bot_member.get("roles", []))
    bot_top_pos = 0
    for r in roles:
        if r["id"] in bot_role_ids and r.get("position", 0) > bot_top_pos:
            bot_top_pos = r["position"]

    if guild.get("owner_id") == bot_user["id"]:
        bot_top_pos = 999999

    log_event(
        "LOAD_SERVER",
        guild_id=payload.guild_id,
        guild_name=guild.get("name", "Unknown"),
        token=payload.token,
        details=f"Loaded {len(channels)} channels, {len(roles)} roles"
    )

    return {
        "guild": {"id": guild["id"], "name": guild["name"], "icon": guild.get("icon")},
        "channels": channels,
        "roles": roles,
        "emojis": emojis,
        "bot_top_position": bot_top_pos
    }


@app.post("/api/deploy-step")
async def deploy_single_step(payload: SingleStepPayload):
    u_hash = get_user_hash(payload.token)
    blocked_reason = is_blacklisted(payload.guild_id, u_hash)
    if blocked_reason:
        return {"status": "error", "detail": f"Access denied: {blocked_reason}"}

    headers = get_headers(payload.token)
    action = payload.action
    t = action.type
    d = action.data

    async with aiohttp.ClientSession() as session:
        try:
            if t == "CREATE_CHANNEL":
                body = {"name": d["name"], "type": d.get("channel_type", 0)}
                if d.get("parent_id"):
                    body["parent_id"] = d["parent_id"]
                if d.get("topic"):
                    body["topic"] = d["topic"]
                if d.get("rate_limit_per_user"):
                    body["rate_limit_per_user"] = int(d["rate_limit_per_user"])
                if d.get("permission_overwrites"):
                    body["permission_overwrites"] = d["permission_overwrites"]
                await discord_request(session, "POST", f"{DISCORD_API}/guilds/{payload.guild_id}/channels", headers, body)

            elif t == "RENAME_CHANNEL":
                await discord_request(session, "PATCH", f"{DISCORD_API}/channels/{action.target_id}", headers, {"name": d["name"]})

            elif t == "EDIT_CHANNEL_META":
                body = {}
                if "topic" in d:
                    body["topic"] = d["topic"]
                if "rate_limit_per_user" in d:
                    body["rate_limit_per_user"] = int(d["rate_limit_per_user"])
                if "nsfw" in d:
                    body["nsfw"] = bool(d["nsfw"])
                await discord_request(session, "PATCH", f"{DISCORD_API}/channels/{action.target_id}", headers, body)

            elif t == "DELETE_CHANNEL":
                await discord_request(session, "DELETE", f"{DISCORD_API}/channels/{action.target_id}", headers)

            elif t == "REORDER_CHANNELS":
                for pc in d.get("parent_changes", []):
                    await discord_request(session, "PATCH", f"{DISCORD_API}/channels/{pc['id']}", headers, {"parent_id": pc["parent_id"]})
                pos_updates = d.get("updates", [])
                if pos_updates:
                    await discord_request(session, "PATCH", f"{DISCORD_API}/guilds/{payload.guild_id}/channels", headers, pos_updates)

            elif t == "EDIT_CHANNEL_PERMS":
                body = {"allow": str(d["allow"]), "deny": str(d["deny"]), "type": d.get("overwrite_type", 0)}
                await discord_request(session, "PUT", f"{DISCORD_API}/channels/{action.target_id}/permissions/{d['overwrite_id']}", headers, body)

            elif t == "SYNC_CATEGORY_PERMS":
                overwrites = d.get("permission_overwrites", [])
                for child_id in d.get("child_ids", []):
                    await discord_request(session, "PATCH", f"{DISCORD_API}/channels/{child_id}", headers, {"permission_overwrites": overwrites})

            elif t == "CREATE_ROLE":
                body = {"name": d["name"], "color": int(d.get("color", "#38bdf8").lstrip("#"), 16)}
                if d.get("permissions") is not None:
                    body["permissions"] = str(d["permissions"])
                if d.get("icon_base64"):
                    body["icon"] = d["icon_base64"]
                await discord_request(session, "POST", f"{DISCORD_API}/guilds/{payload.guild_id}/roles", headers, body)

            elif t == "EDIT_ROLE":
                body = {}
                if "name" in d:
                    body["name"] = d["name"]
                if "color" in d:
                    body["color"] = int(d["color"].lstrip("#"), 16)
                if "permissions" in d:
                    body["permissions"] = str(d["permissions"])
                if d.get("icon_base64"):
                    body["icon"] = d["icon_base64"]
                await discord_request(session, "PATCH", f"{DISCORD_API}/guilds/{payload.guild_id}/roles/{action.target_id}", headers, body)

            elif t == "REORDER_ROLES":
                updates = d.get("updates", [])
                if updates:
                    await discord_request(session, "PATCH", f"{DISCORD_API}/guilds/{payload.guild_id}/roles", headers, updates)

            elif t == "DELETE_ROLE":
                await discord_request(session, "DELETE", f"{DISCORD_API}/guilds/{payload.guild_id}/roles/{action.target_id}", headers)

            elif t == "CREATE_EMOJI":
                body = {"name": d["name"], "image": d["image_base64"]}
                await discord_request(session, "POST", f"{DISCORD_API}/guilds/{payload.guild_id}/emojis", headers, body)

            elif t == "DELETE_EMOJI":
                await discord_request(session, "DELETE", f"{DISCORD_API}/guilds/{payload.guild_id}/emojis/{action.target_id}", headers)

            if payload.is_final:
                log_event(
                    "SYNC_COMPLETE",
                    guild_id=payload.guild_id,
                    guild_name=payload.guild_name or "Unknown",
                    token=payload.token,
                    details=f"Completed batch sync ({payload.total_count} actions)"
                )

            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}


# --- ADMIN PANEL API ENDPOINTS ---

@app.post("/api/admin/data")
async def admin_get_data(payload: AdminAuthPayload):
    verify_admin(payload.username, payload.password)
    conn = sqlite3.connect(DB_PATH)
    unique_users = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM logs WHERE user_hash != '-'").fetchone()[0]
    unique_servers = conn.execute("SELECT COUNT(DISTINCT guild_id) FROM logs WHERE guild_id != '-'").fetchone()[0]
    syncs = conn.execute("SELECT COUNT(*) FROM logs WHERE event_type='SYNC_COMPLETE'").fetchone()[0]

    logs = conn.execute(
        "SELECT id, created_at, guild_name, guild_id, user_hash, event_type, details FROM logs ORDER BY id DESC LIMIT 60"
    ).fetchall()
    blocked = conn.execute(
        "SELECT id, target_value, reason, created_at FROM blacklist ORDER BY id DESC"
    ).fetchall()
    conn.close()

    return {
        "stats": {
            "unique_users": unique_users,
            "unique_servers": unique_servers,
            "syncs": syncs,
            "blacklisted_count": len(blocked)
        },
        "review_webhook": get_review_webhook(),
        "logs": [
            {"id": r[0], "time": r[1], "guild_name": r[2], "guild_id": r[3], "user_hash": r[4], "event": r[5], "details": r[6]}
            for r in logs
        ],
        "blacklist": [
            {"id": b[0], "target": b[1], "reason": b[2], "time": b[3]}
            for b in blocked
        ]
    }


@app.post("/api/admin/webhook/save")
async def admin_save_webhook(payload: AdminWebhookPayload):
    verify_admin(payload.username, payload.password)
    url = payload.webhook_url.strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('review_webhook', ?)", (url,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/admin/webhook/test")
async def admin_test_webhook(payload: AdminWebhookPayload):
    verify_admin(payload.username, payload.password)
    url = payload.webhook_url.strip() or get_review_webhook()
    if not url:
        raise HTTPException(status_code=400, detail="Enter a Discord Webhook URL first.")

    test_body = {
        "username": "Server Planner V1 • Reviews",
        "embeds": [
            {
                "title": "★★★★★  Webhook Connected!",
                "description": "> Test ping from **Server Planner V1 — Becquerel development**. Visitor star ratings and reviews will appear here.",
                "color": 0x38BDF8,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        ]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=test_body) as resp:
            if resp.status >= 400:
                raise HTTPException(status_code=400, detail=f"Discord webhook returned HTTP {resp.status}.")
    return {"ok": True}


@app.post("/api/admin/delete-log")
async def admin_delete_log(payload: AdminDeleteLogPayload):
    verify_admin(payload.username, payload.password)
    conn = sqlite3.connect(DB_PATH)
    if payload.log_id is None:
        conn.execute("DELETE FROM logs")
    else:
        conn.execute("DELETE FROM logs WHERE id = ?", (payload.log_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/admin/blacklist/add")
async def admin_add_blacklist(payload: AdminBlacklistPayload):
    verify_admin(payload.username, payload.password)
    target = payload.target_value.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Target value required.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO blacklist (target_value, reason, created_at) VALUES (?, ?, ?)",
        (target, payload.reason.strip() or "Blacklisted by admin", now)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/admin/blacklist/remove")
async def admin_remove_blacklist(payload: AdminBlacklistPayload):
    verify_admin(payload.username, payload.password)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM blacklist WHERE target_value = ?", (payload.target_value.strip(),))
    conn.commit()
    conn.close()
    return {"ok": True}
