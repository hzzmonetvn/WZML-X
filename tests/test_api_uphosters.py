import asyncio
import sys
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
UPHOSTERS = ROOT / "bot/helper/mirror_leech_utils/uphoster_utils"
PACKAGE = "_test_wz.bot.helper.mirror_leech_utils.uphoster_utils"


def load_uploaders(config):
    # Stub only the application bootstrap; use the real HTTP client and uploader base.
    packages = {}
    segments = PACKAGE.split(".")
    for length in range(1, len(segments) + 1):
        name = ".".join(segments[:length])
        packages[name] = ModuleType(name)
        packages[name].__path__ = [str(UPHOSTERS)] if name == PACKAGE else []
    config_module = ModuleType("_test_wz.bot.core.config_manager")
    config_module.Config = config
    bot_utils = ModuleType("bot.helper.ext_utils.bot_utils")
    bot_utils.SetInterval = lambda *_: SimpleNamespace(cancel=lambda: None)
    packages[config_module.__name__] = config_module
    packages[bot_utils.__name__] = bot_utils
    name = f"{PACKAGE}.api_uploaders"
    spec = spec_from_file_location(name, UPHOSTERS / "api_uploaders.py")
    module = module_from_spec(spec)
    with patch.dict(sys.modules, packages):
        spec.loader.exec_module(module)
    return module


class UploadAPITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.contents = b"abcdefghijklm"
        self.path = Path(temp.name) / "ảnh #1.bin"
        self.path.write_bytes(self.contents)
        self.config = SimpleNamespace(
            FILECLOUD24_ENABLED=True,
            TELECLOUD_PUBLIC_URL="https://share.example.test/cloud",
            TELECLOUD_UPLOAD_PATH="/WZML",
        )
        self.uploaders = load_uploaders(self.config)
        self.results = []
        self.errors = []

    async def server(self, routes):
        app = web.Application()
        app.add_routes(routes)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        return str(server.make_url("/")).rstrip("/")

    def uploader(self, cls, path=None):
        async def completed(*args):
            self.results.append(args)

        async def failed(error):
            self.errors.append(error)

        listener = SimpleNamespace(
            name=self.path.name,
            is_cancelled=False,
            on_upload_complete=completed,
            on_upload_error=failed,
        )
        return cls(listener, path or self.path)

    async def test_filecloud_quick_sends_raw_body_and_encoded_filename(self):
        async def put(request):
            self.assertEqual(request.match_info["name"], self.path.name)
            self.assertEqual(request.headers["Accept"], "application/json")
            self.assertEqual(request.content_length, len(self.contents))
            self.assertEqual(await request.read(), self.contents)
            return web.json_response({"download_url": share}, status=201)

        base = await self.server([web.put("/api/v2/upload/{name}", put)])
        share = f"{base.replace('http:', 'https:')}/download/test"
        with patch.object(self.uploaders, "_FILECLOUD_URL", base):
            uploader = self.uploader(self.uploaders.FileCloud24Upload)
            await uploader.upload()
        self.assertEqual(self.results, [(share, 1, 0, "File")])
        self.assertFalse(self.errors)
        self.assertEqual(uploader.processed_bytes, len(self.contents))

    async def test_filecloud_chunk_retry_sends_same_bytes_and_short_tail(self):
        parts = []
        share = ""

        async def create(request):
            self.assertEqual(
                await request.json(),
                {"filename": self.path.name, "size_bytes": len(self.contents)},
            )
            return web.json_response(
                {
                    "upload_id": "up_1",
                    "upload_token": "test-token",
                    "chunk_size": 5,
                    "chunk_count": 3,
                }
            )

        async def chunk(request):
            self.assertEqual(request.headers["X-Upload-Token"], "test-token")
            self.assertEqual(request.content_type, "application/octet-stream")
            data = await request.read()
            self.assertEqual(request.content_length, len(data))
            parts.append((int(request.match_info["index"]), data))
            if len(parts) == 1:
                return web.Response(status=503, headers={"Retry-After": "0"})
            return web.Response(status=204)

        async def complete(request):
            self.assertEqual(request.headers["X-Upload-Token"], "test-token")
            return web.json_response({"download_url": share})

        base = await self.server(
            [
                web.post("/api/v2/uploads", create),
                web.put("/api/v2/uploads/up_1/chunks/{index}", chunk),
                web.post("/api/v2/uploads/up_1/complete", complete),
            ]
        )
        share = f"{base.replace('http:', 'https:')}/download/test"
        with (
            patch.object(self.uploaders, "_FILECLOUD_URL", base),
            patch.object(self.uploaders, "_QUICK_LIMIT", 1),
        ):
            uploader = self.uploader(self.uploaders.FileCloud24Upload)
            await uploader.upload()
        self.assertEqual(
            parts, [(0, b"abcde"), (0, b"abcde"), (1, b"fghij"), (2, b"klm")]
        )
        self.assertEqual(self.results, [(share, 1, 0, "File")])
        self.assertEqual(uploader.processed_bytes, len(self.contents))
        self.assertFalse(self.errors)

    async def test_filecloud_cancel_aborts_session(self):
        started = asyncio.Event()
        deleted = asyncio.Event()

        async def create(_):
            return web.json_response(
                {
                    "upload_id": "up_1",
                    "upload_token": "test-token",
                    "chunk_size": 5,
                    "chunk_count": 3,
                }
            )

        async def chunk(request):
            await request.read()
            started.set()
            await deleted.wait()
            return web.Response(status=204)

        async def delete(request):
            self.assertEqual(request.headers["X-Upload-Token"], "test-token")
            deleted.set()
            return web.Response(status=204)

        base = await self.server(
            [
                web.post("/api/v2/uploads", create),
                web.put("/api/v2/uploads/up_1/chunks/0", chunk),
                web.delete("/api/v2/uploads/up_1", delete),
            ]
        )
        with (
            patch.object(self.uploaders, "_FILECLOUD_URL", base),
            patch.object(self.uploaders, "_QUICK_LIMIT", 1),
        ):
            uploader = self.uploader(self.uploaders.FileCloud24Upload)
            task = asyncio.create_task(uploader.upload())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(uploader.cancel_task(), 2)
                await asyncio.wait_for(task, 2)
                self.assertTrue(deleted.is_set())
            finally:
                deleted.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.results)
        self.assertEqual(self.errors, ["FileCloud24 upload has been cancelled!"])

    async def test_filecloud_invalid_chunk_size_aborts_session(self):
        closed = []

        async def create(_):
            return web.json_response(
                {
                    "upload_id": "up_1",
                    "upload_token": "test-token",
                    "chunk_size": 0,
                    "chunk_count": 3,
                }
            )

        async def delete(request):
            closed.append(request.headers["X-Upload-Token"])
            return web.Response(status=204)

        base = await self.server(
            [
                web.post("/api/v2/uploads", create),
                web.delete("/api/v2/uploads/up_1", delete),
            ]
        )
        with (
            patch.object(self.uploaders, "_FILECLOUD_URL", base),
            patch.object(self.uploaders, "_QUICK_LIMIT", 1),
        ):
            await self.uploader(self.uploaders.FileCloud24Upload).upload()
        self.assertEqual(closed, ["test-token"])
        self.assertEqual(self.errors, ["Invalid FileCloud24 chunk geometry"])
        self.assertFalse(self.results)

    async def telecloud_server(self, poll, cancel=None):
        async def post(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = await request.multipart()
            values = {}
            async for item in body:
                values[item.name] = (
                    bytes(await item.read())
                    if item.name == "file"
                    else await item.text()
                )
            self.assertEqual(
                values,
                {
                    "file": self.contents,
                    "path": "/WZML",
                    "share": "public",
                    "async": "true",
                },
            )
            return web.json_response({"status": "processing", "task_id": "task_1"})

        routes = [
            web.post("/api/upload-api/upload", post),
            web.get("/api/upload-api/tasks/task_1", poll),
        ]
        if cancel:
            routes.append(web.delete("/api/upload-api/tasks/task_1", cancel))
        return await self.server(routes)

    async def test_telecloud_polls_until_share_token_and_uses_public_url(self):
        statuses = iter(
            [
                {"status": "processing"},
                {"status": "done", "file_id": 12},
                {
                    "status": "done",
                    "file_id": 12,
                    "share_token": "share_1",
                    "share_link": "http://private/s/share_1",
                },
            ]
        )

        async def poll(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            return web.json_response(next(statuses))

        async def no_delay(_):
            await asyncio.sleep(0)

        base = await self.telecloud_server(poll)
        with (
            patch.dict(
                "os.environ", TELECLOUD_API_URL=base, TELECLOUD_API_KEY="test-key"
            ),
            patch.object(self.uploaders, "sleep", no_delay),
        ):
            uploader = self.uploader(self.uploaders.TelecloudUpload)
            await uploader.upload()
        self.assertEqual(
            self.results, [("https://share.example.test/cloud/s/share_1", 1, 0, "File")]
        )
        self.assertFalse(self.errors)
        self.assertEqual(uploader.processed_bytes, len(self.contents))

    async def test_telecloud_cancel_deletes_pending_task(self):
        waiting = asyncio.Event()
        deleted = asyncio.Event()

        async def poll(_):
            waiting.set()
            return web.json_response({"status": "processing"})

        async def cancel(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            deleted.set()
            return web.json_response({"status": "cancelled"})

        base = await self.telecloud_server(poll, cancel)
        with patch.dict(
            "os.environ", TELECLOUD_API_URL=base, TELECLOUD_API_KEY="test-key"
        ):
            uploader = self.uploader(self.uploaders.TelecloudUpload)
            task = asyncio.create_task(uploader.upload())
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                await asyncio.wait_for(uploader.cancel_task(), 2)
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(deleted.is_set())
        self.assertFalse(self.results)
        self.assertEqual(self.errors, ["Telecloud upload has been cancelled!"])

    async def test_telecloud_rejects_redirect_without_forwarding_key(self):
        async def redirect(_):
            raise web.HTTPTemporaryRedirect("/other")

        async def unexpected(_):
            self.fail("Upload followed a redirect")

        base = await self.server(
            [
                web.post("/api/upload-api/upload", redirect),
                web.post("/other", unexpected),
            ]
        )
        with patch.dict(
            "os.environ", TELECLOUD_API_URL=base, TELECLOUD_API_KEY="test-key"
        ):
            await self.uploader(self.uploaders.TelecloudUpload).upload()
        self.assertEqual(self.errors, ["Upload API returned HTTP 307"])
        self.assertFalse(self.results)

    async def test_disabled_and_missing_config_fail_before_upload(self):
        self.config.FILECLOUD24_ENABLED = False
        await self.uploader(self.uploaders.FileCloud24Upload).upload()
        with patch.dict(
            "os.environ", {"TELECLOUD_API_URL": "", "TELECLOUD_API_KEY": ""}
        ):
            await self.uploader(self.uploaders.TelecloudUpload).upload()
        self.assertIn("FileCloud24 is disabled", self.errors[0])
        self.assertIn("requires an API URL", self.errors[1])
        self.assertFalse(self.results)
