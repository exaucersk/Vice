"""Standalone share host: metadata in SQLite, video bytes in Nextcloud.

Run with ``python -m vice.share_host``. This module deliberately has no desktop,
recorder or Nextcloud-account dependencies; the Docker image copies only it and
the package version. Public requests can only read previously registered clips.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import math
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

_SHARE_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_KEY = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{24}\Z")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def base_url(value: str, *, public: bool = False) -> str:
    """Validate operator-supplied origins; public HTTPS may be HTTP in tests."""
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        parsed.port
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        if public and parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            valid = False
        if (not valid or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
    except ValueError:
        raise ValueError("Configure a valid base URL (public URLs require HTTPS).") from None
    return value


@dataclass(frozen=True)
class HostConfig:
    public_url: str
    nextcloud_url: str
    api_token: str
    database: Path
    # DAV needs Nextcloud 29+. 'download' supports legacy /s/token/download.
    media_mode: str = "dav"

    def validate(self) -> None:
        base_url(self.public_url, public=True)
        base_url(self.nextcloud_url)
        if len(self.api_token) < 32 or not self.api_token.isascii() or any(c.isspace() for c in self.api_token):
            raise ValueError("VICE_SHARE_API_TOKEN must contain at least 32 non-whitespace ASCII characters.")
        if self.media_mode not in {"dav", "download"}:
            raise ValueError("NEXTCLOUD_MEDIA_MODE must be dav or download.")


def _record(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object.")
    result = {}
    for name, pattern in (("key", _KEY), ("video_token", _SHARE_TOKEN), ("thumb_token", _SHARE_TOKEN)):
        value = data.get(name)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"Invalid {name}.")
        result[name] = value
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("A clip title of 1 to 200 characters is required.")
    result["title"] = title.strip()
    for name in ("width", "height"):
        value = data.get(name)
        if type(value) is not int or not 1 <= value <= 16384:
            raise ValueError(f"Invalid {name}.")
        result[name] = value
    duration = data.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError("Invalid duration.")
    result["duration"] = duration
    color = data.get("color", "#0099ff")
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise ValueError("Invalid embed color.")
    result["color"] = color
    return result


def _page(cfg: HostConfig, clip_id: str, data: dict) -> str:
    origin = cfg.public_url.rstrip("/")
    title = html.escape(data["title"], quote=True)
    page = html.escape(f"{origin}/c/{clip_id}", quote=True)
    video = html.escape(f"{origin}/v/{clip_id}.mp4", quote=True)
    thumb = html.escape(f"{origin}/t/{clip_id}.jpg", quote=True)
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="{data['color']}">
<meta property="og:site_name" content="Vice"><meta property="og:type" content="video.other">
<meta property="og:title" content="{title}"><meta property="og:description" content="Clipped with Vice">
<meta property="og:url" content="{page}"><meta property="og:image" content="{thumb}">
<meta property="og:video" content="{video}"><meta property="og:video:url" content="{video}">
<meta property="og:video:secure_url" content="{video}"><meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="{data['width']}"><meta property="og:video:height" content="{data['height']}">
<meta name="twitter:card" content="player"><meta name="twitter:player" content="{page}">
<meta name="twitter:player:width" content="{data['width']}"><meta name="twitter:player:height" content="{data['height']}">
<meta name="twitter:player:stream" content="{video}"><meta name="twitter:player:stream:content_type" content="video/mp4">
<meta name="twitter:image" content="{thumb}">
<style>body{{margin:0;background:#101114;color:white;display:grid;place-items:center;min-height:100vh}}
video{{max-width:100%;max-height:100vh}}</style></head>
<body><video src="{video}" poster="{thumb}" controls playsinline preload="metadata"></video></body></html>'''


def create_app(cfg: HostConfig) -> web.Application:
    cfg.validate()
    cfg = replace(cfg, public_url=base_url(cfg.public_url, public=True), nextcloud_url=base_url(cfg.nextcloud_url))
    app = web.Application(client_max_size=16 * 1024)
    database_key = web.AppKey("database", sqlite3.Connection)
    session_key = web.AppKey("session", ClientSession)

    async def resources(application):
        cfg.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(cfg.database)
        os.chmod(cfg.database, 0o600)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
        db.commit()
        application[database_key] = db
        session = ClientSession(auto_decompress=False, headers={"Accept-Encoding": "identity"}, timeout=ClientTimeout(
            total=None, sock_connect=15, sock_read=120,
        ))
        application[session_key] = session
        try:
            yield
        finally:
            await session.close()
            db.close()

    app.cleanup_ctx.append(resources)

    def authorize(req):
        expected = f"Bearer {cfg.api_token}".encode()
        if not hmac.compare_digest(req.headers.get("Authorization", "").encode(), expected):
            raise web.HTTPUnauthorized(headers={"Cache-Control": "no-store"})

    def upstream(token):
        # Tokens are validated at registration; no client-supplied host or
        # arbitrary URL is ever fetched by this service.
        base = cfg.nextcloud_url.rstrip("/")
        if cfg.media_mode == "download":
            return f"{base}/s/{token}/download"
        return f"{base}/public.php/dav/files/{token}"

    def find(req):
        clip_id = req.match_info["id"]
        if not _ID.fullmatch(clip_id):
            raise web.HTTPNotFound()
        row = req.app[database_key].execute("SELECT data FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        return clip_id, json.loads(row[0])

    async def health(req):
        return web.json_response({"service": "vice-share", "api": 1}, headers={"Cache-Control": "no-store"})

    async def api_health(req):
        authorize(req)
        return await health(req)

    async def register(req):
        authorize(req)
        try:
            data = _record(await req.json())
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid clip metadata or share tokens."}, status=400)
        db = req.app[database_key]
        row = db.execute("SELECT id, data FROM clips WHERE key = ?", (data["key"],)).fetchone()
        if row:
            previous = json.loads(row[1])
            if any(previous[k] != data[k] for k in ("video_token", "thumb_token")):
                return web.json_response({"error": "A different clip already uses this key."}, status=409)
            clip_id = row[0]
        else:
            # Check public accessibility before announcing a usable link. Do
            # not follow redirects into an auth page or a different origin.
            try:
                for name, content_type in (("video_token", "video/mp4"), ("thumb_token", "image/jpeg")):
                    async with req.app[session_key].head(upstream(data[name]), allow_redirects=False,
                                                         timeout=ClientTimeout(total=20)) as response:
                        if response.status != 200 or response.content_type != content_type:
                            return web.json_response({"error": "Nextcloud media is not publicly readable. Check its share policy, media mode and origin URL."}, status=502)
            except (ClientError, asyncio.TimeoutError):
                return web.json_response({"error": "The share host could not reach Nextcloud."}, status=502)
            clip_id = secrets.token_urlsafe(18)
            db.execute("INSERT OR IGNORE INTO clips VALUES (?, ?, ?)", (clip_id, data["key"], json.dumps(data)))
            db.commit()
            # Concurrent retries may have registered the same key during the
            # upstream checks. Always return the one durable database row.
            row = db.execute("SELECT id, data FROM clips WHERE key = ?", (data["key"],)).fetchone()
            if any(json.loads(row[1])[k] != data[k] for k in ("video_token", "thumb_token")):
                return web.json_response({"error": "A different clip already uses this key."}, status=409)
            clip_id = row[0]
        return web.json_response({"id": clip_id, "url": f"{cfg.public_url.rstrip('/')}/c/{clip_id}"},
                                 headers={"Cache-Control": "no-store"})

    async def delete(req):
        authorize(req)
        clip_id, _ = find(req)
        req.app[database_key].execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        req.app[database_key].commit()
        return web.Response(status=204)

    async def page(req):
        clip_id, data = find(req)
        return web.Response(text=_page(cfg, clip_id, data), content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })

    async def media(req):
        _, data = find(req)
        is_video = req.path.startswith("/v/")
        token, content_type = (data["video_token"], "video/mp4") if is_video else (data["thumb_token"], "image/jpeg")
        headers = {name: req.headers[name] for name in ("Range", "If-Range") if name in req.headers}
        response = None
        try:
            async with req.app[session_key].request(req.method, upstream(token), headers=headers, allow_redirects=False) as origin:
                if origin.status in {404, 410}:
                    raise web.HTTPNotFound()
                if origin.status == 416:
                    return web.Response(status=416, headers={"Content-Range": origin.headers.get("Content-Range", "bytes */0"),
                                                            "Cache-Control": "no-store"})
                if origin.status not in {200, 206} or origin.content_type != content_type or origin.headers.get("Content-Encoding"):
                    raise web.HTTPBadGateway(text="Nextcloud media is unavailable.")
                forwarded = {name: origin.headers[name] for name in (
                    "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified",
                ) if name in origin.headers}
                forwarded.update({"Content-Type": content_type, "Cache-Control": "no-store",
                                  "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})
                response = web.StreamResponse(status=origin.status, headers=forwarded)
                await response.prepare(req)
                if req.method != "HEAD":
                    async for block in origin.content.iter_chunked(128 * 1024):
                        await response.write(block)
                await response.write_eof()
                return response
        except (ClientError, asyncio.TimeoutError, ConnectionError):
            if response is not None and response.prepared:
                response.force_close()
                if req.transport:
                    req.transport.close()
                return response
            raise web.HTTPBadGateway(text="Could not read Nextcloud media.") from None

    app.router.add_get("/healthz", health)
    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/clips", register)
    app.router.add_delete("/api/clips/{id}", delete)
    app.router.add_get("/c/{id}", page)
    app.router.add_get("/v/{id}.mp4", media)
    app.router.add_get("/t/{id}.jpg", media)
    return app


def main() -> None:
    cfg = HostConfig(
        public_url=os.environ.get("PUBLIC_BASE_URL", ""),
        nextcloud_url=os.environ.get("NEXTCLOUD_BASE_URL", ""),
        api_token=os.environ.get("VICE_SHARE_API_TOKEN", ""),
        database=Path(os.environ.get("DATABASE_PATH", "/data/clips.sqlite3")),
        media_mode=os.environ.get("NEXTCLOUD_MEDIA_MODE", "dav"),
    )
    try:
        app = create_app(cfg)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None)


if __name__ == "__main__":
    main()
