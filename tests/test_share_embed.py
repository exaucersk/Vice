import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_nextcloud
from vice.config import SharingConfig
from vice.nextcloud import NextcloudError, validate_settings
from vice.share_embed import ShareHostClient, prepare_embed, publish_embed
from vice.share_host import HostConfig, create_app


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class EmbedPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = test_nextcloud.NextcloudUploadTests()
        await self.fake.asyncSetUp()
        self.addCleanup(self.fake.tmp.cleanup)
        self.addAsyncCleanup(self.fake.runner.cleanup)
        self.root = Path(self.fake.tmp.name)
        self.cache = self.root / "cache"
        self.source = self.root / "recording.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-threads", "2", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(self.source),
        ], check=True)
        self.fake.clip = self.source
        self.cfg = self.fake.cfg
        self.cfg.provider = "nextcloud_embed"
        self.cfg.share_host_api_token = "host-token-not-nextcloud-password-123456789"
        self.host_cfg = HostConfig("https://gclips.kimonima.de", self.fake.base,
                                   self.cfg.share_host_api_token, self.root / "host.sqlite3")
        self.host = await self.start_host()
        self.stages = []

    async def start_host(self):
        host = TestClient(TestServer(create_app(self.host_cfg)))
        await host.start_server()
        self.addAsyncCleanup(host.close)
        self.cfg.share_host_url = str(host.make_url("")).rstrip("/")
        return host

    async def progress(self, stage):
        self.stages.append(stage)

    async def test_full_pipeline_preserves_original_and_produces_playable_faststart_video(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        validate_settings(self.cfg)
        await ShareHostClient(self.cfg).check()
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        raw = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(prepared.video)])
        streams = json.loads(raw)["streams"]
        self.assertEqual([s["codec_name"] for s in streams], ["h264", "aac"])
        self.assertEqual(streams[0]["pix_fmt"], "yuv420p")
        video_bytes = prepared.video.read_bytes()
        self.assertLess(video_bytes.find(b"moov"), video_bytes.find(b"mdat"))
        url = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        self.assertEqual(self.stages, ["upload", "register"])
        self.assertEqual(len(self.fake.files), 3, "original, browser video and thumbnail are separate")
        self.assertIn(self.source.read_bytes(), self.fake.files.values())
        response = await self.host.get(urlsplit(url).path)
        self.assertEqual(response.status, 200)
        self.assertIn("https://gclips.kimonima.de/v/", await response.text())
        clip_id = url.rsplit("/", 1)[1]
        response = await self.host.get(f"/v/{clip_id}.mp4", headers={"Range": "bytes=0-63"})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), video_bytes[:64])

    async def test_cache_and_remote_uploads_are_reused_after_rename_and_host_restart(self):
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        first = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        encoded_mtime = prepared.video.stat().st_mtime_ns
        upload_count = sum(method == "PUT" for method, _ in self.fake.calls)
        self.source = self.source.rename(self.root / "new name.mkv")
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        self.assertEqual(prepared.video.stat().st_mtime_ns, encoded_mtime)
        await self.host.close()
        self.host = await self.start_host()
        second = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        self.assertEqual(urlsplit(first).path, urlsplit(second).path)
        self.assertEqual(sum(method == "PUT" for method, _ in self.fake.calls), upload_count)
        self.source.unlink()
        self.assertEqual((await self.host.get(urlsplit(second).path)).status, 200)

    async def test_rebuilding_a_deleted_local_cache_keeps_the_remote_file_and_link(self):
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        first = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        shutil.rmtree(self.cache)
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        second = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        self.assertEqual(second, first)
        self.assertEqual(sum(method == "PUT" for method, _ in self.fake.calls), 3)

    async def test_a_new_recording_gets_a_different_link_without_replacing_the_old_one(self):
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        first = await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        second = await publish_embed(self.cfg, self.source, "newrecording", prepared, self.progress)
        self.assertNotEqual(first, second)
        self.assertEqual((await self.host.get(urlsplit(first).path)).status, 200)
        self.assertEqual((await self.host.get(urlsplit(second).path)).status, 200)

    async def test_bad_host_key_fails_preflight_without_uploads(self):
        self.cfg.share_host_api_token = "wrong-token-which-is-still-at-least-32-characters"
        with self.assertRaisesRegex(NextcloudError, "API token"):
            await ShareHostClient(self.cfg).check()
        self.assertEqual(self.fake.files, {})

    async def test_source_modified_after_preparation_does_not_publish(self):
        prepared = await prepare_embed(self.source, "abcdefghijkl", self.cache)
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(NextcloudError, "changed"):
            await publish_embed(self.cfg, self.source, "abcdefghijkl", prepared, self.progress)
        self.assertEqual(self.fake.files, {})

    async def test_upstream_html_or_redirect_is_not_treated_as_a_working_host(self):
        async def response(req):
            return web.Response(status=302, headers={"Location": "https://unrelated.invalid"})
        app = web.Application()
        app.router.add_get("/api/health", response)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.cfg.share_host_url = str(server.make_url("")).rstrip("/")
        with self.assertRaises(NextcloudError):
            await ShareHostClient(self.cfg).check()
