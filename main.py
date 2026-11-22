import os
import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

app = FastAPI(title="Server Planner V1")
DISCORD_API = "https://discord.com/api/v10"
DB_PATH = "analytics.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "becquerel2026")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            guild_name TEXT,
            user_hash TEXT,
            details TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def log_event(event_type: str, guild_name: str = "-", token: str = "", details: str = ""):
    user_hash = hashlib.sha256(token.strip().encode()).hexdigest()[:10] if token else "-"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (event_type, guild_name, user_hash, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, guild_name, user_hash, details, now)
    )
    conn.commit()
    conn.close()


class AuthPayload(BaseModel):
    token: str
    guild_id: str


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


@app.post("/api/load")
async def load_guild(payload: AuthPayload):
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
    """Executes one staged action at a time so the UI can drive a real-time progress bar."""
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
                    guild_name=payload.guild_name or "Unknown",
                    token=payload.token,
                    details=f"Completed batch sync ({payload.total_count} actions)"
                )

            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized. Append ?key=YOUR_ADMIN_KEY to URL.")

    conn = sqlite3.connect(DB_PATH)
    unique_users = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM logs WHERE user_hash != '-'").fetchone()[0]
    unique_servers = conn.execute("SELECT COUNT(DISTINCT guild_name) FROM logs WHERE guild_name != '-'").fetchone()[0]
    syncs = conn.execute("SELECT COUNT(*) FROM logs WHERE event_type='SYNC_COMPLETE'").fetchone()[0]
    rows = conn.execute("SELECT created_at, guild_name, user_hash, event_type, details FROM logs ORDER BY id DESC LIMIT 40").fetchall()
    conn.close()

    table_rows = "".join([
        f"<tr><td>{r[0]}</td><td><strong>{r[1]}</strong></td><td><code>{r[2]}</code></td><td><span class='tag'>{r[3]}</span></td><td>{r[4]}</td></tr>"
        for r in rows
    ])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Admin - Server Planner V1</title>
      <style>
        body {{ background:#050507; color:#f4f5f8; font-family:Inter,sans-serif; padding:40px; max-width:1050px; margin:0 auto; }}
        h1 {{ font-weight:600; font-size:22px; margin-bottom:24px; }}
        .stats {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; margin-bottom:28px; }}
        .card {{ background:#0d0e12; border:1px solid rgba(255,255,255,0.1); padding:20px 24px; border-radius:14px; }}
        .num {{ font-size:30px; font-weight:600; color:#38bdf8; }}
        .lbl {{ font-size:12px; color:#8a909e; margin-top:6px; }}
        table {{ width:100%; border-collapse:collapse; background:#0d0e12; border-radius:14px; overflow:hidden; font-size:13px; }}
        th, td {{ padding:14px 16px; text-align:left; border-bottom:1px solid rgba(255,255,255,0.07); }}
        th {{ color:#8a909e; font-size:11px; text-transform:uppercase; letter-spacing:0.6px; }}
        .tag {{ background:rgba(56,189,248,0.14); color:#38bdf8; padding:4px 9px; border-radius:6px; font-size:11px; }}
      </style>
    </head>
    <body>
      <h1>Becquerel development — Usage Overview</h1>
      <div class="stats">
        <div class="card"><div class="num">{unique_users}</div><div class="lbl">Unique Users</div></div>
        <div class="card"><div class="num">{unique_servers}</div><div class="lbl">Servers Connected</div></div>
        <div class="card"><div class="num">{syncs}</div><div class="lbl">Batch Syncs Run</div></div>
      </div>
      <table>
        <thead><tr><th>Time</th><th>Server Name</th><th>User ID (Hash)</th><th>Activity</th><th>Summary</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </body>
    </html>
    """
