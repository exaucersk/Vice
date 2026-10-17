"""Upload immutable clip copies and create read-only Nextcloud file shares.

Only called by an explicit Share action. The public file and share live in
Nextcloud, so local renames, deletions and Vice restarts do not remove them.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from .config import SharingConfig


class NextcloudError(Exception):
    """An actionable failure safe to show without leaking credentials."""


def validate_settings(cfg: SharingConfig) -> None:
    if cfg.provider not in {"builtin", "nextcloud", "nextcloud_embed"}:
        raise NextcloudError("Unknown sharing provider.")
    if cfg.provider == "builtin":
        return
    _server_url(cfg.nextcloud_url)
    if not cfg.nextcloud_username.strip() or not cfg.nextcloud_app_password:
        raise NextcloudError("Enter your Nextcloud username and app password.")
    if ":" in cfg.nextcloud_username:
        raise NextcloudError("A Nextcloud username cannot contain a colon.")
    _folder_parts(cfg.nextcloud_folder)
    if cfg.provider == "nextcloud_embed":
        _server_url(cfg.share_host_url)
        if (len(cfg.share_host_api_token) < 32 or not cfg.share_host_api_token.isascii()
                or any(c.isspace() for c in cfg.share_host_api_token)):
            raise NextcloudError("Enter the share host's API token (at least 32 characters).")


def _server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate an explicitly supplied port too.
        secure = parsed.scheme == "https"
        loopback = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1",
        }
        if (not (secure or loopback) or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise NextcloudError("Enter the HTTPS base URL of your Nextcloud, without a file or share path.") from None
    return value


def _folder_parts(value: str) -> list[str]:
    parts = value.strip().strip("/").split("/")
    if any(not part or part in {".", ".."} or "\\" in part
           or any(ord(c) < 32 for c in part) for part in parts):
        raise NextcloudError("Enter a Nextcloud folder such as Vice or Videos/Vice.")
    return parts


def share_key(cfg: SharingConfig, path: Path, token: str) -> tuple:
    """Stable across a local rename, different after trimming or re-recording."""
    st = path.stat()
    host = cfg.share_host_url.strip().rstrip("/") if cfg.provider == "nextcloud_embed" else ""
    return (cfg.provider, host,
            cfg.nextcloud_url.strip().rstrip("/"), cfg.nextcloud_username.strip(),
            cfg.nextcloud_folder.strip().strip("/"), token, st.st_size, st.st_mtime_ns)


def _check_status(status: int, operation: str, accepted: set[int]) -> None:
    if status in accepted:
        return
    hint = {
        401: "Check your Nextcloud username and app password.",
        403: "Check folder permissions and whether public link sharing is allowed.",
        413: "The clip exceeds your Nextcloud or reverse proxy upload limit.",
        507: "Your Nextcloud storage is full.",
    }.get(status, "Check your Nextcloud connection and server settings, then retry.")
    raise NextcloudError(f"Nextcloud could not {operation} (HTTP {status}). {hint}")


async def _ocs(session, method: str, url: str, *, query=None, **kwargs):
    async with session.request(
        method, url, params={"format": "json", **(query or {})},
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        allow_redirects=False, **kwargs,
    ) as response:
        _check_status(response.status, "create or retrieve the file link", {200, 201})
        try:
            body = await response.json(content_type=None)
            ocs = body["ocs"]
            if int(ocs["meta"]["statuscode"]) not in {100, 200}:
                # Do not echo upstream messages: they may contain private data.
                raise NextcloudError("Nextcloud refused the public file link. Check its sharing policy.")
            return ocs["data"]
        except (KeyError, TypeError, ValueError):
            raise NextcloudError("Nextcloud returned an invalid sharing response.") from None


def _public_url(share: dict) -> str:
    url = share.get("url", "")
    if not isinstance(url, str):
        raise NextcloudError("Nextcloud did not return a public file link.")
    try:
        return _server_url(url)
    except NextcloudError:
        raise NextcloudError("Nextcloud did not return a valid public file link.") from None


async def share_video(cfg: SharingConfig, path: Path, token: str) -> str:
    return await share_file(cfg, path, token)


async def share_file(cfg: SharingConfig, path: Path, token: str,
                     *, revision: tuple[int, int] | None = None) -> str:
    """Stream a clip without buffering it in RAM; reuse a completed upload.

    Token and file revision make the remote filename independent of sequential
    clip names. Never overwrite an earlier public video with a different clip.
    The caller serializes requests to avoid creating duplicate public shares.
    """
    validate_settings(cfg)
    base = _server_url(cfg.nextcloud_url)
    folders = _folder_parts(cfg.nextcloud_folder)
    key = share_key(cfg, path, token)
    size, mtime = key[-2:]
    remote_size, remote_mtime = revision if revision is not None else (size, mtime)
    suffix = path.suffix.lower()
    content_type = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".jpg": "image/jpeg"}.get(suffix)
    if not content_type:
        raise NextcloudError("This file type cannot be shared through Nextcloud.")
    filename = f"{token}-{remote_size}-{remote_mtime}{suffix}"
    remote_path = "/" + "/".join([*folders, filename])
    dav = f"{base}/remote.php/dav/files/{quote(cfg.nextcloud_username.strip(), safe='')}"
    file_url = dav + quote(remote_path, safe="/")
    shares_url = f"{base}/ocs/v2.php/apps/files_sharing/api/v1/shares"

    async def chunks(file):
        while block := await asyncio.to_thread(file.read, 1024 * 1024):
            yield block

    try:
        auth = aiohttp.BasicAuth(cfg.nextcloud_username.strip(), cfg.nextcloud_app_password,
                                 encoding="utf-8")
        timeout = aiohttp.ClientTimeout(total=3600, sock_connect=30, sock_read=300)
        async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
            folder_url = dav
            for folder in folders:
                folder_url += "/" + quote(folder, safe="")
                async with session.request("MKCOL", folder_url, allow_redirects=False) as response:
                    _check_status(response.status, "create the clip folder", {201, 405})

            async with session.head(file_url, allow_redirects=False) as response:
                _check_status(response.status, "check the uploaded clip", {200, 404})
                exists = response.status == 200
            if not exists:
                with path.open("rb") as file:
                    st = os.fstat(file.fileno())
                    if (st.st_size, st.st_mtime_ns) != (size, mtime):
                        raise NextcloudError("The clip changed before upload. Please share it again.")
                    async with session.put(
                        file_url, data=chunks(file), allow_redirects=False,
                        headers={"Content-Length": str(size), "If-None-Match": "*",
                                 "Content-Type": content_type},
                    ) as response:
                        _check_status(response.status, "upload the clip", {201, 204, 412})
                    after = os.fstat(file.fileno())
                    if (after.st_size, after.st_mtime_ns) != (size, mtime):
                        raise NextcloudError("The clip changed during upload. Please share it again.")

            shares = await _ocs(session, "GET", shares_url, query={"path": remote_path})
            if not isinstance(shares, list):
                raise NextcloudError("Nextcloud returned an invalid list of file links.")
            for share in shares:
                if (isinstance(share, dict) and str(share.get("share_type")) == "3"
                        # Nextcloud reports a read-only link as 17 (read + share).
                        and str(share.get("permissions")) in {"1", "17"}
                        and not share.get("password")):
                    return _public_url(share)
            share = await _ocs(session, "POST", shares_url, data={
                "path": remote_path, "shareType": "3", "permissions": "1",
            })
            if not isinstance(share, dict):
                raise NextcloudError("Nextcloud did not return a public file link.")
            return _public_url(share)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise NextcloudError("Could not reach Nextcloud securely or the upload timed out. Check the URL and connection, then retry.") from None
    except OSError:
        raise NextcloudError("The local clip could not be read. Please share it again.") from None
