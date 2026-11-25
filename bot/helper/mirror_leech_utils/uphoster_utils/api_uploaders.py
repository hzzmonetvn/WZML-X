from asyncio import (
    CancelledError,
    Lock,
    current_task,
    gather,
    sleep,
    timeout,
    to_thread,
)
from contextlib import aclosing, suppress
from mimetypes import guess_type
from os import getenv
from pathlib import Path
from re import fullmatch
from urllib.parse import quote, urlsplit

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, FormData

from ....core.config_manager import Config
from .base import BaseUpload
from .common import ProgressFileReader

_FILECLOUD_URL = "https://filecloud24.cc"
_QUICK_LIMIT = 100_000_000
_FILECLOUD_LOCK = Lock()


def _url(value, schemes):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme in schemes
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and (parts.port is None or parts.port > 0)
            and not any(char.isspace() or ord(char) < 32 for char in value)
        ):
            return parts
    except ValueError:
        pass
    raise ValueError("Invalid upload service URL")


def _base_url(value, schemes):
    parts = _url(value, schemes)
    if parts.query or parts.fragment:
        raise ValueError("Upload service base URL cannot include a query or fragment")


def validate_api_uphoster(service):
    if service == "filecloud24" and not Config.FILECLOUD24_ENABLED:
        raise ValueError(
            "FileCloud24 is disabled. Ask the bot administrator to enable it."
        )
    if service == "telecloud":
        key = getenv("TELECLOUD_API_KEY", "").strip()
        api_url = getenv("TELECLOUD_API_URL", "").strip()
        if not (api_url and Config.TELECLOUD_PUBLIC_URL and key):
            raise ValueError(
                "Telecloud requires an API URL, public URL and TELECLOUD_API_KEY."
            )
        _base_url(api_url, ("http", "https"))
        _base_url(Config.TELECLOUD_PUBLIC_URL, ("https",))
        if not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("Invalid TELECLOUD_API_KEY format")


class _HTTPError(Exception):
    def __init__(self, status, retry_after):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Upload API returned HTTP {status}")


async def _request(session, method, url, *, read_json=True, **kwargs):
    async with session.request(
        method, url, allow_redirects=False, **kwargs
    ) as response:
        if not 200 <= response.status < 300:
            raise _HTTPError(response.status, response.headers.get("Retry-After", ""))
        if read_json:
            data = await response.json()
            if not isinstance(data, dict):
                raise ValueError("Unexpected upload API response")
            return data


def _identifier(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise ValueError(f"Invalid {key} in upload API response")
    return value


class _APIUpload(BaseUpload):
    def _resolve_token(self):
        return None

    async def upload(self):
        self._task = current_task()
        try:
            if not self.listener.is_cancelled:
                await super().upload()
        except CancelledError:
            if not self.listener.is_cancelled:
                raise
        finally:
            self._task = None

    async def _validate_token(self):
        validate_api_uphoster(self.SERVICE_NAME.lower())

    async def _upload_process(self):
        file = Path(self._path)
        if not file.is_file():
            raise ValueError(
                f"{self.SERVICE_NAME} accepts a single file; use -z for a folder."
            )
        try:
            async with timeout(2 * 60 * 60):
                async with ClientSession(
                    timeout=ClientTimeout(total=None, connect=15)
                ) as session:
                    link = await self.upload_file(session, file)
        except TimeoutError:
            raise ValueError(f"{self.SERVICE_NAME} upload timed out") from None
        if not self.listener.is_cancelled:
            await self.listener.on_upload_complete(link, 1, 0, "File")

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if task := getattr(self, "_task", None):
            if not task.cancelling():
                task.cancel()
            await gather(task, return_exceptions=True)
        await super().cancel_task()


class FileCloud24Upload(_APIUpload):
    SERVICE_NAME = "FileCloud24"

    async def upload_file(self, session, file):
        size = file.stat().st_size
        async with _FILECLOUD_LOCK:
            if not size:
                raise ValueError("FileCloud24 cannot upload an empty file")
            if size <= _QUICK_LIMIT:
                with ProgressFileReader(file, self._progress_callback) as body:
                    result = await _request(
                        session,
                        "PUT",
                        f"{_FILECLOUD_URL}/api/v2/upload/{quote(file.name, safe='')}",
                        data=body,
                        headers={
                            "Accept": "application/json",
                            "Content-Length": str(size),
                        },
                    )
            else:
                result = await self._upload_chunks(session, file, size)
        link = result.get("download_url")
        if (
            not isinstance(link, str)
            or _url(link, ("https",)).netloc != urlsplit(_FILECLOUD_URL).netloc
        ):
            raise ValueError("FileCloud24 did not return a share URL")
        self._progress_callback(size)
        return link

    async def _upload_chunks(self, session, file, size):
        endpoint = f"{_FILECLOUD_URL}/api/v2/uploads"
        upload = await _request(
            session, "POST", endpoint, json={"filename": file.name, "size_bytes": size}
        )
        upload_id = _identifier(upload, "upload_id")
        token = upload.get("upload_token")
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or not token.isprintable()
        ):
            raise ValueError("Invalid FileCloud24 upload token")
        endpoint = f"{endpoint}/{upload_id}"
        headers = {"X-Upload-Token": token}
        sent = 0
        try:
            chunk_size = upload.get("chunk_size")
            count = upload.get("chunk_count")
            if (
                type(chunk_size) is not int
                or chunk_size <= 0
                or type(count) is not int
                or count != (size + chunk_size - 1) // chunk_size
            ):
                raise ValueError("Invalid FileCloud24 chunk geometry")
            for index in range(count):
                length = min(chunk_size, size - sent)
                for attempt in range(3):
                    try:
                        async with aclosing(
                            self._read_chunk(file, sent, length)
                        ) as body:
                            await _request(
                                session,
                                "PUT",
                                f"{endpoint}/chunks/{index}",
                                read_json=False,
                                data=body,
                                headers={
                                    **headers,
                                    "Content-Length": str(length),
                                    "Content-Type": "application/octet-stream",
                                },
                            )
                        break
                    except (_HTTPError, ClientConnectionError, TimeoutError) as exc:
                        if (
                            attempt == 2
                            or isinstance(exc, _HTTPError)
                            and exc.status not in (429, 500, 502, 503, 504)
                        ):
                            raise
                        retry_after = (
                            exc.retry_after if isinstance(exc, _HTTPError) else ""
                        )
                        delay = (
                            int(retry_after)
                            if retry_after.isascii() and retry_after.isdecimal()
                            else 2**attempt
                        )
                        if delay > 60:
                            raise
                        await sleep(delay)
                sent += length
                self._progress_callback(sent)
            result = await _request(
                session, "POST", f"{endpoint}/complete", headers=headers
            )
        except (Exception, CancelledError):
            with suppress(Exception):
                async with timeout(10):
                    await _request(
                        session, "DELETE", endpoint, read_json=False, headers=headers
                    )
            raise
        return result

    @staticmethod
    async def _read_chunk(file, offset, length):
        # Each attempt owns its file cursor so an interrupted read cannot affect a retry.
        with file.open("rb") as source:
            source.seek(offset)
            while length:
                data = await to_thread(source.read, min(length, 256 * 1024))
                if not data:
                    raise OSError("File ended while uploading a chunk")
                length -= len(data)
                yield data


class TelecloudUpload(_APIUpload):
    SERVICE_NAME = "Telecloud"

    async def upload_file(self, session, file):
        api = f"{getenv('TELECLOUD_API_URL', '').strip().rstrip('/')}/api/upload-api"
        public_url = Config.TELECLOUD_PUBLIC_URL.rstrip("/")
        headers = {"Authorization": f"Bearer {getenv('TELECLOUD_API_KEY', '').strip()}"}
        size = file.stat().st_size
        with ProgressFileReader(file, self._progress_callback) as body:
            form = FormData()
            form.add_field(
                "file",
                body,
                filename=file.name,
                content_type=guess_type(file.name)[0] or "application/octet-stream",
            )
            form.add_field("path", Config.TELECLOUD_UPLOAD_PATH or "/")
            form.add_field("share", "public")
            form.add_field("async", "true")
            upload = await _request(
                session, "POST", f"{api}/upload", data=form, headers=headers
            )
        task_id = _identifier(upload, "task_id")
        task_url = f"{api}/tasks/{task_id}"
        try:
            share_waits = 0
            poll_timeout = ClientTimeout(total=20, connect=15)
            while True:
                task = await _request(
                    session, "GET", task_url, headers=headers, timeout=poll_timeout
                )
                status = task.get("status")
                if status == "done":
                    if task.get("share_token"):
                        token = _identifier(task, "share_token")
                        self._progress_callback(size)
                        return f"{public_url}/s/{token}"
                    # Telecloud can mark storage done before it creates the share token.
                    share_waits += 1
                    if share_waits == 4:
                        raise ValueError(
                            "Telecloud stored the file but did not create a share link"
                        )
                elif status in ("error", "cancelled", "canceled"):
                    raise ValueError(
                        f"Telecloud task {status} before completing upload"
                    )
                elif not isinstance(status, str) or not status:
                    raise ValueError("Invalid Telecloud task status")
                await sleep(2)
        except (Exception, CancelledError):
            with suppress(Exception):
                async with timeout(10):
                    await _request(
                        session, "DELETE", task_url, read_json=False, headers=headers
                    )
            raise
