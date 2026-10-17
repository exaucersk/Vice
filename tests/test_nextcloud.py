import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import BasicAuth, web

from vice.config import Config, SharingConfig, load, save
from vice.nextcloud import NextcloudError, share_video, validate_settings


class NextcloudUploadTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual HTTP requests against a local WebDAV/OCS server."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = Path(self.tmp.name) / "A #clip ü.mp4"
        self.clip.write_bytes(b"video" * 300_000)
        self.files = {}
        self.folders = set()
        self.shares = {}
        self.calls = []
        self.fail_method = None
        self.fail_status = 500
        self.ocs_status = 100
        self.invalid_json = False
        self.change_during_upload = False
        app = web.Application(client_max_size=5 * 1024 * 1024)
        app.router.add_route("*", "/nc/remote.php/dav/files/{user}/{path:.*}", self.dav)
        app.router.add_route("*", "/nc/ocs/v2.php/apps/files_sharing/api/v1/shares", self.ocs)
        app.router.add_get("/nc/public.php/dav/files/{token}", self.public_file)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}/nc"
        self.cfg = SharingConfig(
            provider="nextcloud", nextcloud_url=self.base,
            nextcloud_username="alice ü", nextcloud_app_password="app-secret",
            nextcloud_folder="Videos/Clips #1",
        )

    def check_auth(self, req):
        auth = BasicAuth.decode(req.headers["Authorization"], encoding="utf-8")
        self.assertEqual((auth.login, auth.password), ("alice ü", "app-secret"))
        self.calls.append((req.method, req.path))

    async def dav(self, req):
        self.check_auth(req)
        self.assertEqual(req.match_info["user"], "alice ü")
        if req.method == self.fail_method:
            return web.Response(status=self.fail_status, headers={"Location": "https://wrong.invalid"})
        path = "/" + req.match_info["path"]
        if req.method == "MKCOL":
            exists = path in self.folders
            self.folders.add(path)
            return web.Response(status=405 if exists else 201)
        if req.method == "HEAD":
            return web.Response(status=200 if path in self.files else 404)
        if req.method == "PUT":
            self.assertEqual(req.headers["If-None-Match"], "*")
            data = await req.read()
            self.assertEqual(int(req.headers["Content-Length"]), len(data))
            self.assertEqual(req.content_type, {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".jpg": "image/jpeg"}[Path(path).suffix])
            self.files[path] = data
            if self.change_during_upload:
                self.clip.write_bytes(b"modified")
            return web.Response(status=201)
        self.fail(f"Unexpected DAV method {req.method}")

    async def ocs(self, req):
        self.check_auth(req)
        self.assertEqual(req.headers["OCS-APIRequest"], "true")
        self.assertEqual(req.query["format"], "json")
        if self.invalid_json:
            return web.Response(text="<html>login</html>", content_type="text/html")
        if req.method == "GET":
            data = self.shares.get(req.query["path"], [])
        elif req.method == "POST":
            body = await req.post()
            self.assertEqual(body["shareType"], "3")
            self.assertEqual(body["permissions"], "1")
            self.assertIn(body["path"], self.files)
            # Real servers add the share bit (16) to a requested read-only link.
            data = {"share_type": 3, "permissions": 17, "password": None,
                    "url": f"{self.base}/s/public0000000{len(self.shares)}"}
            self.shares.setdefault(body["path"], []).append(data)
        else:
            self.fail(f"Unexpected OCS method {req.method}")
        return web.json_response({"ocs": {"meta": {"statuscode": self.ocs_status}, "data": data}})

    async def public_file(self, req):
        self.assertNotIn("Authorization", req.headers)
        token = req.match_info["token"]
        for remote_path, shares in self.shares.items():
            if any(share["url"].endswith("/" + token) for share in shares):
                file = Path(self.tmp.name) / (token + Path(remote_path).suffix)
                if not file.exists():
                    file.write_bytes(self.files[remote_path])
                return web.FileResponse(file)
        raise web.HTTPNotFound()

    async def test_uploads_bytes_and_shares_only_the_single_file_read_only(self):
        url = await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.assertEqual(url, f"{self.base}/s/public00000000")
        self.assertEqual(list(self.files.values()), [self.clip.read_bytes()])
        self.assertEqual(len(self.shares), 1)
        remote = next(iter(self.shares))
        self.assertTrue(remote.startswith("/Videos/Clips #1/abcdefghijkl-"))
        self.assertTrue(remote.endswith(".mp4"))

    async def test_an_unchanged_clip_reuses_upload_and_share_even_after_local_rename(self):
        first = await share_video(self.cfg, self.clip, "abcdefghijkl")
        renamed = self.clip.with_name("new name.mp4")
        self.clip.rename(renamed)
        self.assertEqual(await share_video(self.cfg, renamed, "abcdefghijkl"), first)
        self.assertEqual(sum(m == "PUT" for m, _ in self.calls), 1)
        self.assertEqual(sum(m == "POST" for m, _ in self.calls), 1)

    async def test_modified_and_reused_clips_leave_previous_public_copies_intact(self):
        old_bytes = self.clip.read_bytes()
        first = await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.clip.write_bytes(b"trimmed")
        second = await share_video(self.cfg, self.clip, "abcdefghijkl")
        third = await share_video(self.cfg, self.clip, "newrecording")
        self.assertEqual(len({first, second, third}), 3)
        self.assertIn(old_bytes, self.files.values())
        self.clip.unlink()
        self.assertEqual(len(self.files), 3)

    async def test_a_failed_upload_creates_no_public_link_and_can_be_retried(self):
        self.fail_method, self.fail_status = "PUT", 413
        with self.assertRaisesRegex(NextcloudError, "upload limit"):
            await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.assertEqual(self.shares, {})
        self.fail_method = None
        self.assertTrue(await share_video(self.cfg, self.clip, "abcdefghijkl"))

    async def test_does_not_follow_redirects_with_credentials_or_publish_a_link(self):
        self.fail_method, self.fail_status = "MKCOL", 301
        with self.assertRaisesRegex(NextcloudError, "HTTP 301"):
            await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.files, {})

    async def test_reports_ocs_failures_even_when_http_succeeded(self):
        self.ocs_status = 403
        with self.assertRaisesRegex(NextcloudError, "sharing policy"):
            await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.assertEqual(self.shares, {})

    async def test_rejects_html_instead_of_an_ocs_response(self):
        self.invalid_json = True
        with self.assertRaisesRegex(NextcloudError, "invalid sharing response"):
            await share_video(self.cfg, self.clip, "abcdefghijkl")

    async def test_does_not_publish_a_clip_modified_during_upload(self):
        self.change_during_upload = True
        with self.assertRaisesRegex(NextcloudError, "changed during upload"):
            await share_video(self.cfg, self.clip, "abcdefghijkl")
        self.assertEqual(self.shares, {})


class NextcloudConfigTests(unittest.TestCase):
    def config(self):
        return SharingConfig(provider="nextcloud", nextcloud_url="https://cloud.example/nc",
                             nextcloud_username="alice", nextcloud_app_password="secret")

    def test_requires_secure_base_url_credentials_and_a_safe_folder(self):
        for field, values in {
            "nextcloud_url": ["http://cloud.example", "https://user:secret@cloud.example",
                              "https://cloud.example/?x=y", "https://cloud.example/#hash", ""],
            "nextcloud_folder": ["../Videos", "Videos/../private", "a//b", "", "a\\b"],
            "nextcloud_username": ["", "alice:bob"], "nextcloud_app_password": [""],
            "provider": ["unknown"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    cfg = self.config()
                    setattr(cfg, field, value)
                    with self.assertRaises(NextcloudError):
                        validate_settings(cfg)
        validate_settings(self.config())
        validate_settings(SharingConfig())

    def test_saving_credentials_restricts_existing_config_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.touch(mode=0o644)
            with mock.patch("vice.config.CONFIG_DIR", Path(tmp)), mock.patch("vice.config.CONFIG_PATH", path):
                cfg = Config(sharing=self.config())
                save(cfg)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(load().sharing, cfg.sharing)
                self.assertNotIn("secret", repr(cfg))
