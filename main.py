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
ADMIN_KEY = os.environ.get("ADMIN_KEY", "becquerel2026")  # Override in Render Environment Variables


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            guild_id TEXT,
            guild_name TEXT,
            user_hash TEXT,
            ip_address TEXT,
            details TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def log_event(event_type: str, ip: str, guild_id: str = "-", guild_name: str = "-", token: str = "", details: str = ""):
    user_hash = hashlib.sha256(token.strip().encode()).hexdigest()[:12] if token else "-"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (event_type, guild_id, guild_name, user_hash, ip_address, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_type, guild_id, guild_name, user_hash, ip, details, now)
    )
    conn.commit()
    conn.close()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthPayload(BaseModel):
    token: str
    guild_id: str


class ActionItem(BaseModel):
    id: str
    type: str
    target_id: Optional[str] = None
    data: Dict[str, Any] = {}


class DeployPayload(BaseModel):
    token: str
    guild_id: str
    actions: List[ActionItem]


def get_headers(token: str) -> Dict[str, str]:
    clean_token = token.strip()
    if not clean_token.startswith("Bot "):
        clean_token = f"Bot {clean_token}"
    return {
        "Authorization": clean_token,
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": "Server Planner V1 — Becquerel development"
    }


async def discord_request(session: aiohttp.ClientSession, method: str, url: str, headers: dict, json_data: dict = None):
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
async def serve_ui(request: Request):
    log_event("PAGE_VISIT", get_client_ip(request), details="Opened Server Planner V1")
    return FileResponse("ui.html")


@app.post("/api/load")
async def load_guild(payload: AuthPayload, request: Request):
    headers = get_headers(payload.token)
    async with aiohttp.ClientSession() as session:
        guild = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}", headers)
        channels = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/channels", headers)
        roles = await discord_request(session, "GET", f"{DISCORD_API}/guilds/{payload.guild_id}/roles", headers)

    channels.sort(key=lambda c: c.get("position", 0))
    roles.sort(key=lambda r: r.get("position", 0), reverse=True)

    log_event(
        "LOAD_SERVER",
        get_client_ip(request),
        guild_id=payload.guild_id,
        guild_name=guild.get("name", "Unknown"),
        token=payload.token,
        details=f"Loaded {len(channels)} channels, {len(roles)} roles"
    )

    return {
        "guild": {"id": guild["id"], "name": guild["name"], "icon": guild.get("icon")},
        "channels": channels,
        "roles": roles
    }


@app.post("/api/deploy")
async def deploy_changes(payload: DeployPayload, request: Request):
    headers = get_headers(payload.token)
    results = []

    async with aiohttp.ClientSession() as session:
        for action in payload.actions:
            t = action.type
            d = action.data
            try:
                if t == "CREATE_CHANNEL":
                    body = {"name": d["name"], "type": d.get("channel_type", 0)}
                    if d.get("parent_id"):
                        body["parent_id"] = d["parent_id"]
                    await discord_request(session, "POST", f"{DISCORD_API}/guilds/{payload.guild_id}/channels", headers, body)
                elif t == "RENAME_CHANNEL":
                    await discord_request(session, "PATCH", f"{DISCORD_API}/channels/{action.target_id}", headers, {"name": d["name"]})
                elif t == "DELETE_CHANNEL":
                    await discord_request(session, "DELETE", f"{DISCORD_API}/channels/{action.target_id}", headers)
                elif t == "EDIT_CHANNEL_PERMS":
                    body = {"allow": str(d["allow"]), "deny": str(d["deny"]), "type": d.get("overwrite_type", 0)}
                    await discord_request(session, "PUT", f"{DISCORD_API}/channels/{action.target_id}/permissions/{d['overwrite_id']}", headers, body)
                elif t == "CREATE_ROLE":
                    body = {"name": d["name"], "color": int(d.get("color", "#d96b43").lstrip("#"), 16)}
                    if d.get("icon_base64"):
                        body["icon"] = d["icon_base64"]
                    await discord_request(session, "POST", f"{DISCORD_API}/guilds/{payload.guild_id}/roles", headers, body)
                elif t == "EDIT_ROLE":
                    body = {}
                    if "name" in d:
                        body["name"] = d["name"]
                    if "color" in d:
                        body["color"] = int(d["color"].lstrip("#"), 16)
                    if d.get("icon_base64"):
                        body["icon"] = d["icon_base64"]
                    await discord_request(session, "PATCH", f"{DISCORD_API}/guilds/{payload.guild_id}/roles/{action.target_id}", headers, body)
                elif t == "DELETE_ROLE":
                    await discord_request(session, "DELETE", f"{DISCORD_API}/guilds/{payload.guild_id}/roles/{action.target_id}", headers)

                results.append({"id": action.id, "status": "success"})
            except Exception as e:
                results.append({"id": action.id, "status": "error", "detail": str(e)})

    log_event(
        "DEPLOY_QUEUE",
        get_client_ip(request),
        guild_id=payload.guild_id,
        token=payload.token,
        details=f"Executed {len(payload.actions)} staged actions"
    )
    return {"results": results}


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized. Append ?key=YOUR_ADMIN_KEY to URL.")

    conn = sqlite3.connect(DB_PATH)
    visits = conn.execute("SELECT COUNT(*) FROM logs WHERE event_type='PAGE_VISIT'").fetchone()[0]
    unique_users = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM logs WHERE user_hash != '-'").fetchone()[0]
    deploys = conn.execute("SELECT COUNT(*) FROM logs WHERE event_type='DEPLOY_QUEUE'").fetchone()[0]
    rows = conn.execute("SELECT event_type, guild_name, guild_id, user_hash, ip_address, details, created_at FROM logs ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()

    table_rows = "".join([
        f"<tr><td>{r[6]}</td><td><span class='tag'>{r[0]}</span></td><td>{r[1]} ({r[2]})</td><td><code>{r[3]}</code></td><td>{r[4]}</td><td>{r[5]}</td></tr>"
        for r in rows
    ])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Admin — Server Planner V1</title>
      <style>
        body {{ background:#110e0c; color:#f2ede7; font-family:Inter,sans-serif; padding:32px; }}
        h1 {{ font-weight:500; margin-bottom:20px; }}
        .stats {{ display:flex; gap:16px; margin-bottom:24px; }}
        .card {{ background:#1b1715; border:1px solid rgba(255,245,235,0.1); padding:18px 24px; border-radius:14px; min-width:180px; }}
        .num {{ font-size:28px; font-weight:600; color:#d96b43; }}
        .lbl {{ font-size:12px; color:#9c9288; margin-top:4px; }}
        table {{ width:100%; border-collapse:collapse; background:#1b1715; border-radius:14px; overflow:hidden; font-size:13px; }}
        th, td {{ padding:12px 14px; text-align:left; border-bottom:1px solid rgba(255,245,235,0.07); }}
        th {{ color:#9c9288; font-size:11px; text-transform:uppercase; }}
        .tag {{ background:rgba(217,107,67,0.16); color:#d96b43; padding:3px 8px; border-radius:6px; font-size:11px; }}
      </style>
    </head>
    <body>
      <h1>Becquerel development — Management Panel</h1>
      <div class="stats">
        <div class="card"><div class="num">{visits}</div><div class="lbl">Total Page Visits</div></div>
        <div class="card"><div class="num">{unique_users}</div><div class="lbl">Unique Bot Users</div></div>
        <div class="card"><div class="num">{deploys}</div><div class="lbl">Total Deployments</div></div>
      </div>
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Server</th><th>User Hash</th><th>IP</th><th>Details</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </body>
    </html>
    """
