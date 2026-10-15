"""Prepare and publish a browser-compatible copy to the standalone host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

from .config import SharingConfig
from .media import communicate_with_timeout
from .nextcloud import NextcloudError, _server_url, share_file

PROFILE = "embed-v1"


@dataclass(frozen=True)
class PreparedEmbed:
    video: Path
    thumbnail: Path
    width: int
    height: int
    duration: float
    revision: tuple[int, int]


def _revision(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


async def _run(command: list[str], *, timeout: float) -> bytes:
    proc = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    try:
        proc = await asyncio.shield(spawn)
        output, _ = await communicate_with_timeout(proc, timeout)
    except (OSError, asyncio.TimeoutError):
        raise NextcloudError("Could not prepare the video. Check ffmpeg or try a shorter clip.") from None
    finally:
        if proc is None:
            try:
                proc = await spawn
            except OSError:
                pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
    if proc.returncode:
        raise NextcloudError("Could not encode a browser-compatible clip. Check that the source video is readable.")
    return output or b""


async def _probe(path: Path) -> tuple[dict, dict | None, float]:
    raw = await _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout=20)
    try:
        data = json.loads(raw)
        video = next(stream for stream in data["streams"] if stream.get("codec_type") == "video")
        audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
        if not 0 < duration <= 86400 or not video.get("width") or not video.get("height"):
            raise ValueError
        return video, audio, duration
    except (ValueError, KeyError, StopIteration, TypeError):
        raise NextcloudError("The clip's video metadata could not be read.") from None


async def prepare_embed(path: Path, token: str, cache_dir: Path) -> PreparedEmbed:
    revision = _revision(path)
    key = hashlib.sha256(f"{PROFILE}:{token}:{revision}".encode()).hexdigest()
    folder = cache_dir / "share-embeds" / key
    folder.mkdir(parents=True, exist_ok=True)
    video_path, thumbnail = folder / "video.mp4", folder / "poster.jpg"
    if not video_path.is_file() or video_path.stat().st_size == 0:
        video, _, _ = await _probe(path)
        try:
            source_rate = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "30"))
        except (ValueError, ZeroDivisionError):
            source_rate = math.inf
        rate = min(60.0, source_rate) if math.isfinite(source_rate) and source_rate > 0 else 30.0
        copy_video = (video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
                      and video["width"] <= 1920 and video["height"] <= 1080
                      and 0 < source_rate <= 60)
        encoder = ["-c:v", "copy"] if copy_video else [
            "-vf", f"scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps={rate:g}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "8M", "-bufsize", "16M",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.2", "-threads:v", "2",
        ]
        handle = tempfile.NamedTemporaryFile(prefix=".video-", suffix=".mp4", dir=folder, delete=False)
        temp = Path(handle.name)
        handle.close()
        try:
            await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2", "-filter_threads", "1",
                        "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", *encoder,
                        "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-map_metadata", "-1",
                        "-movflags", "+faststart", "-y", str(temp)], timeout=1800)
            if _revision(path) != revision:
                raise NextcloudError("The clip changed during preparation. Please share it again.")
            temp.replace(video_path)
        finally:
            temp.unlink(missing_ok=True)
    video, audio, duration = await _probe(video_path)
    if video.get("codec_name") != "h264" or (audio and audio.get("codec_name") != "aac"):
        raise NextcloudError("The prepared video is not H.264/AAC.")
    if not thumbnail.is_file() or thumbnail.stat().st_size == 0:
        handle = tempfile.NamedTemporaryFile(prefix=".poster-", suffix=".jpg", dir=folder, delete=False)
        temp = Path(handle.name)
        handle.close()
        try:
            await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
                        "-ss", str(min(1, duration / 2)), "-i", str(video_path), "-frames:v", "1",
                        "-vf", "scale=640:-2", "-q:v", "4", "-y", str(temp)], timeout=30)
            temp.replace(thumbnail)
        finally:
            temp.unlink(missing_ok=True)
    if _revision(path) != revision:
        raise NextcloudError("The clip changed during preparation. Please share it again.")
    return PreparedEmbed(video_path, thumbnail, video["width"], video["height"], duration, revision)


def _token(url: str) -> str:
    match = re.search(r"/s/([A-Za-z0-9_-]{8,128})/?$", urlsplit(url).path)
    if not match:
        raise NextcloudError("Nextcloud did not return a usable file share token.")
    return match.group(1)


class ShareHostClient:
    def __init__(self, cfg: SharingConfig):
        self.url = _server_url(cfg.share_host_url)
        self.token = cfg.share_host_api_token

    async def _request(self, method: str, path: str, *, data=None) -> dict:
        try:
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as session:
                async with session.request(method, self.url + path, json=data, allow_redirects=False) as response:
                    if response.status == 401:
                        raise NextcloudError("The share host rejected the API token. Check it in Settings.")
                    if response.status not in {200, 201}:
                        raise NextcloudError("The share host could not register the clip. Check its Nextcloud origin, media mode and public access.")
                    payload = await response.json(content_type=None)
                    if not isinstance(payload, dict):
                        raise ValueError
                    return payload
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise NextcloudError("Could not reach the share host securely. Check its URL, tunnel route and API token.") from None

    async def check(self) -> None:
        data = await self._request("GET", "/api/health")
        if data.get("service") != "vice-share" or data.get("api") != 1:
            raise NextcloudError("This URL does not point to a compatible Vice share host.")

    async def register(self, payload: dict) -> str:
        data = await self._request("POST", "/api/clips", data=payload)
        clip_id = data.get("id", "")
        if not isinstance(clip_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", clip_id):
            raise NextcloudError("The share host did not return a clip ID.")
        # Build from the trusted configured base, never from an upstream URL.
        return f"{self.url}/c/{clip_id}"


async def publish_embed(cfg: SharingConfig, path: Path, token: str, prepared: PreparedEmbed,
                        progress: Callable[[str], Awaitable[None]]) -> str:
    if _revision(path) != prepared.revision:
        raise NextcloudError("The clip changed. Please share it again.")
    await progress("upload")
    # Preserve the original upload. Derivatives have separate stable filenames
    # based on the source revision, so cache rebuilds do not overwrite originals
    # or create a fresh Nextcloud file for the same unchanged recording.
    await share_file(cfg, path, token)
    video_url = await share_file(cfg, prepared.video, f"{token}-{PROFILE}", revision=prepared.revision)
    thumb_url = await share_file(cfg, prepared.thumbnail, f"{token}-{PROFILE}", revision=prepared.revision)
    if _revision(path) != prepared.revision:
        raise NextcloudError("The clip changed during upload. Please share it again.")
    video_token, thumb_token = _token(video_url), _token(thumb_url)
    key = hashlib.sha256(json.dumps([cfg.nextcloud_url, cfg.nextcloud_username, token,
                                    prepared.revision, PROFILE, video_token, thumb_token]).encode()).hexdigest()
    await progress("register")
    return await ShareHostClient(cfg).register({
        "key": key, "video_token": video_token, "thumb_token": thumb_token,
        "title": path.stem[:200], "width": prepared.width, "height": prepared.height,
        "duration": prepared.duration, "color": cfg.embed_color,
    })
