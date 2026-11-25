import ast
import asyncio
import hashlib
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[1] / "bot/helper/ext_utils/hyperdl_utils.py"
CHUNK = 64 * 1024
MB = 1024 * 1024


class MemoryBudget:
    async def reserve(self, _size):
        pass

    async def release(self, _size):
        pass


def load_downloader(source=None):
    # Importing bot starts its event loop and creates log files. Execute the
    # downloader with stdlib imports and stub out Telegram/bootstrap dependencies.
    tree = ast.parse(MODULE.read_text() if source is None else source)
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.ImportFrom)
        or not (node.level or node.module.startswith(("aiofiles", "pyrogram")))
    ]

    async def makedirs(path, exist_ok):
        Path(path).mkdir(parents=True, exist_ok=exist_ok)

    async def no_sleep(_seconds):
        await asyncio.sleep(0)

    scope = {
        "HypertgTransfer": object,
        "allowed_cpus": lambda: [0],
        "budget": MemoryBudget(),
        "LOGGER": logging.getLogger("test.hyperdl"),
        "MB": MB,
        "makedirs": makedirs,
        **{
            name: type(name, (Exception,), {})
            for name in ("FloodWait", "FloodPremiumWait", "StopTransmission")
        },
    }
    exec(compile(tree, str(MODULE), "exec"), scope)
    scope["sleep"] = no_sleep
    return scope["HypertgDownload"]


def make_downloader(cls, directory, data, clients=3, request=None, window=4):
    dl = cls.__new__(cls)
    dl._cancel = asyncio.Event()
    dl._tasks = []
    dl._obj = SimpleNamespace(_processed_bytes=0)
    dl._listener = SimpleNamespace(transmission_mode="bot")
    dl.work_loads = dict.fromkeys(range(1, clients + 1), 0)
    dl.clients = {
        idx: SimpleNamespace(me=SimpleNamespace(username=f"client{idx}"))
        for idx in dl.work_loads
    }
    dl.directory = directory
    dl.file_name = "payload.bin"
    dl.file_size = len(data)
    dl.num_parts = clients
    dl.pipeline_depth = window
    dl.chunk_size = CHUNK
    dl._cdn_info = {}
    dl._cdn_sessions = {}

    async def noop(*_args):
        pass

    async def ref(*_args):
        return SimpleNamespace(dc_id=1)

    async def fetch(_session, _client, _location, offset, size, _attempt):
        if request:
            return await request(offset, size)
        return data[offset : offset + size]

    dl._warmup = dl._close_all = dl._get_session = noop
    dl._fetch_ref = ref
    dl._location = lambda _fid: None
    dl._do_req = fetch
    return dl


class HyperDownloadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "payload.bin"
        self.data = bytes(range(251)) * 14_000 + b"tail"
        self.downloader = load_downloader()

    async def assert_download(self, dl):
        path = await dl.handle_download()
        self.assertEqual(Path(path), self.output)
        self.assertEqual(
            hashlib.sha256(self.output.read_bytes()).digest(),
            hashlib.sha256(self.data).digest(),
        )
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    async def test_download_covers_tail_with_out_of_order_chunks(self):
        async def request(offset, size):
            if offset % (3 * CHUNK) == 0:
                await asyncio.sleep(0.001)
            return self.data[offset : offset + size]

        dl = make_downloader(self.downloader, self.temp.name, self.data, request=request)
        await self.assert_download(dl)
        self.assertEqual(dl._obj._processed_bytes, len(self.data))

    async def test_retries_short_response(self):
        calls = 0

        async def request(offset, size):
            nonlocal calls
            if offset == CHUNK:
                calls += 1
                if calls == 1:
                    return self.data[offset : offset + size - 1]
            return self.data[offset : offset + size]

        dl = make_downloader(self.downloader, self.temp.name, self.data, request=request)
        await self.assert_download(dl)
        self.assertEqual(calls, 2)

    async def test_does_not_publish_if_an_offset_cannot_be_downloaded(self):
        async def request(offset, size):
            if offset == CHUNK:
                return b""
            return self.data[offset : offset + size]

        dl = make_downloader(self.downloader, self.temp.name, self.data, request=request)
        with self.assertRaises(OSError):
            await dl.handle_download()
        self.assertEqual(list(self.output.parent.iterdir()), [])

    async def test_retry_preserves_other_ranges_at_chunk_boundary(self):
        failed_offset = (len(self.data) // 3) // CHUNK * CHUNK
        calls = 0

        async def request(offset, size):
            nonlocal calls
            if offset == failed_offset:
                calls += 1
                if calls == 1:
                    return b""
            return self.data[offset : offset + size]

        dl = make_downloader(self.downloader, self.temp.name, self.data, request=request)
        await self.assert_download(dl)
        self.assertEqual(calls, 2)
        self.assertEqual(dl._obj._processed_bytes, len(self.data))

    async def test_cancel_during_retry_drains_workers_and_removes_staging(self):
        started = asyncio.Event()
        drained = asyncio.Event()
        calls = 0

        async def request(offset, size):
            nonlocal calls
            if offset == CHUNK:
                calls += 1
                if calls == 1:
                    return b""
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    drained.set()
            return self.data[offset : offset + size]

        dl = make_downloader(self.downloader, self.temp.name, self.data, request=request)
        task = asyncio.create_task(dl.handle_download())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            dl._cancel.set()
            for part in dl._tasks:
                part.cancel()
            self.assertIsNone(await asyncio.wait_for(task, timeout=2))
            self.assertTrue(drained.is_set())
            self.assertEqual(list(self.output.parent.iterdir()), [])
            self.assertEqual(sum(dl.work_loads.values()), 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_does_not_replace_existing_file_on_write_error(self):
        self.output.write_bytes(b"existing file")
        dl = make_downloader(self.downloader, self.temp.name, self.data)
        write = dl._pwrite

        async def fail_write(fd, data, offset):
            if offset == CHUNK:
                raise OSError("test write error")
            await write(fd, data, offset)

        dl._pwrite = fail_write
        with self.assertRaises(OSError):
            await dl.handle_download()
        self.assertEqual(self.output.read_bytes(), b"existing file")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
