import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vice.share_host import HostConfig, create_app


class ShareHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(bytes(range(256)) * 100)
        self.thumb = self.root / "poster.jpg"
        self.thumb.write_bytes(b"jpeg")
        self.upstream_requests = []
        self.upstream_status = 200

        async def media(req):
            self.upstream_requests.append(dict(req.headers))
            if self.upstream_status != 200:
                return web.Response(status=self.upstream_status, headers={"Location": "https://unrelated.invalid"})
            path = {"videotoken123": self.video, "thumbtoken123": self.thumb}.get(req.match_info["token"])
            if path is None or not path.exists():
                raise web.HTTPNotFound()
            return web.FileResponse(path)

        origin = web.Application()
        origin.router.add_get("/nc/public.php/dav/files/{token}", media)
        origin.router.add_get("/nc/s/{token}/download", media)
        self.origin = TestServer(origin)
        await self.origin.start_server()
        self.addAsyncCleanup(self.origin.close)
        self.cfg = HostConfig("https://gclips.kimonima.de", str(self.origin.make_url("/nc")), "private-key-" * 4,
                              self.root / "clips.sqlite3")
        self.client = await self.start_host()
        self.headers = {"Authorization": f"Bearer {self.cfg.api_token}"}
        self.data = {"key": "a" * 64, "video_token": "videotoken123", "thumb_token": "thumbtoken123",
                     "title": 'A clip <script>alert("x")</script>', "width": 1280, "height": 720,
                     "duration": 20, "color": "#0099ff"}

    async def start_host(self, cfg=None):
        client = TestClient(TestServer(create_app(cfg or self.cfg)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def register(self, data=None):
        response = await self.client.post("/api/clips", json=data or self.data, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_registration_requires_authentication_before_any_upstream_request(self):
        for headers in ({}, {"Authorization": "Bearer wrong-key"}):
            response = await self.client.post("/api/clips", json=self.data, headers=headers)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.upstream_requests, [])
        self.assertEqual((await self.client.get("/api/health")).status, 401)
        response = await self.client.get("/api/health", headers=self.headers)
        self.assertEqual((await response.json())["service"], "vice-share")

    async def test_public_page_has_only_canonical_escaped_metadata_and_opaque_ids(self):
        registered = await self.register()
        self.assertRegex(registered["id"], r"^[A-Za-z0-9_-]{24}$")
        self.assertEqual(registered["url"], "https://gclips.kimonima.de/c/" + registered["id"])
        response = await self.client.get("/c/" + registered["id"], headers={"Host": "injected.invalid"})
        page = await response.text()
        self.assertIn("https://gclips.kimonima.de/v/" + registered["id"] + ".mp4", page)
        self.assertIn('property="og:video:type" content="video/mp4"', page)
        self.assertIn('name="twitter:player:stream"', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        for secret in ("injected.invalid", self.cfg.api_token, self.cfg.nextcloud_url, "videotoken123", "thumbtoken123"):
            self.assertNotIn(secret, page)
        for path in ("/", "/api/clips", "/c/video", "/v/video.mp4", "/t/poster.jpg"):
            self.assertIn((await self.client.get(path)).status, (404, 405))

    async def test_range_head_and_thumbnail_stream_without_credentials_or_buffering_whole_video(self):
        clip_id = (await self.register())["id"]
        response = await self.client.get(f"/v/{clip_id}.mp4", headers={"Range": "bytes=3-8", "Authorization": "Bearer visitor-secret"})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.video.read_bytes()[3:9])
        self.assertEqual(response.headers["Content-Range"], f"bytes 3-8/{self.video.stat().st_size}")
        self.assertEqual(response.headers["Content-Type"], "video/mp4")
        self.assertEqual(response.headers["Content-Disposition"], "inline")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(all("Authorization" not in headers for headers in self.upstream_requests))
        response = await self.client.head(f"/v/{clip_id}.mp4")
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers["Content-Length"]), self.video.stat().st_size)
        self.assertEqual(await response.read(), b"")
        response = await self.client.get(f"/t/{clip_id}.jpg")
        self.assertEqual(await response.read(), b"jpeg")
        response = await self.client.get(f"/v/{clip_id}.mp4", headers={"Range": "bytes=999999-"})
        self.assertEqual(response.status, 416)

    async def test_duplicate_and_concurrent_shares_keep_the_same_durable_link_after_restart(self):
        first, second = await asyncio.gather(self.register(), self.register())
        self.assertEqual(first, second)
        await self.client.close()
        self.client = await self.start_host()
        self.assertEqual(await self.register(), first)
        self.assertEqual((await self.client.get("/c/" + first["id"])).status, 200)
        self.assertEqual(self.cfg.database.stat().st_mode & 0o777, 0o600)

    async def test_rejects_token_path_traversal_urls_and_invalid_metadata(self):
        for field, value in (("video_token", "../../private"), ("thumb_token", "https://internal.invalid/a"),
                             ("width", True), ("height", -1), ("duration", float("nan")),
                             ("color", '\" onclick=\"x'), ("key", "bad")):
            data = {**self.data, field: value}
            response = await self.client.post("/api/clips", json=data, headers=self.headers)
            self.assertEqual(response.status, 400, (field, await response.text()))
        self.assertEqual(self.upstream_requests, [])

    async def test_unreadable_or_redirected_upstream_does_not_create_a_link(self):
        self.upstream_status = 302
        response = await self.client.post("/api/clips", json=self.data, headers=self.headers)
        self.assertEqual(response.status, 502)
        self.assertEqual(len(self.upstream_requests), 1)
        self.upstream_status = 200
        clip_id = (await self.register())["id"]
        self.video.unlink()
        self.assertEqual((await self.client.get(f"/v/{clip_id}.mp4")).status, 404)

    async def test_revoking_a_link_is_authenticated_and_persistent(self):
        clip_id = (await self.register())["id"]
        self.assertEqual((await self.client.delete("/api/clips/" + clip_id)).status, 401)
        self.assertEqual((await self.client.delete("/api/clips/" + clip_id, headers=self.headers)).status, 204)
        self.assertEqual((await self.client.get("/c/" + clip_id)).status, 404)
        self.assertTrue(self.video.exists())

    async def test_same_key_cannot_replace_an_earlier_video(self):
        await self.register()
        response = await self.client.post("/api/clips", headers=self.headers, json={**self.data, "video_token": "other-token-123"})
        self.assertEqual(response.status, 409)

    async def test_legacy_nextcloud_download_route_is_configurable(self):
        from dataclasses import replace
        await self.client.close()
        self.client = await self.start_host(replace(self.cfg, media_mode="download"))
        clip_id = (await self.register())["id"]
        response = await self.client.get(f"/v/{clip_id}.mp4")
        self.assertEqual(await response.read(), self.video.read_bytes())
