"""Streaming ZIP (with automatic ZIP64 support for >4 GB archives) over ObjectStore keys."""

import asyncio
import re
import struct
import time
import zlib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

from loguru import logger

from src.storage.objects import ObjectStore

ZIP32_LIMIT = 0xFFFFFFFF
ZIP16_LIMIT = 0xFFFF
UTF8_FLAG = 0x0800
EXTERNAL_ATTR_FILE = 0o100644 << 16


def natural_key(text: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _dos_datetime(ts: float | None = None) -> tuple[int, int]:
    t = time.localtime(ts) if ts else time.localtime()
    year = max(1980, min(2107, t.tm_year))
    dos_date = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return dos_time, dos_date


@dataclass
class _CentralEntry:
    name_bytes: bytes
    dos_time: int
    dos_date: int
    crc32: int
    size: int
    header_offset: int


def _local_header(name_bytes: bytes, dos_time: int, dos_date: int, crc32: int, size: int) -> bytes:
    if size >= ZIP32_LIMIT:
        extra = struct.pack("<HHQQ", 0x0001, 16, size, size)
        version = 45
        size32 = ZIP32_LIMIT
    else:
        extra = b""
        version = 20
        size32 = size
    return (
        struct.pack(
            "<4sHHHHHIIIHH",
            b"PK\x03\x04",
            version,
            UTF8_FLAG,
            0,  # ZIP_STORED
            dos_time,
            dos_date,
            crc32,
            size32,
            size32,
            len(name_bytes),
            len(extra),
        )
        + name_bytes
        + extra
    )


def _central_header(entry: _CentralEntry) -> bytes:
    extra_parts: list[bytes] = []
    if entry.size >= ZIP32_LIMIT:
        size32 = ZIP32_LIMIT
        extra_parts.append(struct.pack("<QQ", entry.size, entry.size))
    else:
        size32 = entry.size

    if entry.header_offset >= ZIP32_LIMIT:
        offset32 = ZIP32_LIMIT
        extra_parts.append(struct.pack("<Q", entry.header_offset))
    else:
        offset32 = entry.header_offset

    if extra_parts:
        payload = b"".join(extra_parts)
        extra = struct.pack("<HH", 0x0001, len(payload)) + payload
        version_needed = 45
    else:
        extra = b""
        version_needed = 20

    return (
        struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",
            45,  # version made by
            version_needed,
            UTF8_FLAG,
            0,  # ZIP_STORED
            entry.dos_time,
            entry.dos_date,
            entry.crc32,
            size32,
            size32,
            len(entry.name_bytes),
            len(extra),
            0,  # comment length
            0,  # disk number start
            0,  # internal attrs
            EXTERNAL_ATTR_FILE,
            offset32,
        )
        + entry.name_bytes
        + extra
    )


def _end_of_central_dir(count: int, cd_size: int, cd_offset: int) -> bytes:
    need_zip64 = count >= ZIP16_LIMIT or cd_size >= ZIP32_LIMIT or cd_offset >= ZIP32_LIMIT
    parts: list[bytes] = []
    if need_zip64:
        zip64_eocd_offset = cd_offset + cd_size
        parts.append(
            struct.pack(
                "<4sQHHIIQQQQ",
                b"PK\x06\x06",
                44,  # size of remaining record
                45,
                45,
                0,
                0,
                count,
                count,
                cd_size,
                cd_offset,
            )
        )
        parts.append(
            struct.pack(
                "<4sIQI",
                b"PK\x06\x07",
                0,
                zip64_eocd_offset,
                1,
            )
        )
    parts.append(
        struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            min(count, ZIP16_LIMIT),
            min(count, ZIP16_LIMIT),
            min(cd_size, ZIP32_LIMIT),
            min(cd_offset, ZIP32_LIMIT),
            0,
        )
    )
    return b"".join(parts)


async def stream_zip(
    objects: ObjectStore,
    items: list[tuple[str, str]],
    concurrency: int = 6,
) -> AsyncIterator[bytes]:
    """
    Stream a valid ZIP (or ZIP64) archive containing `(arcname, object_key)` pairs.
    Prefetches up to `concurrency` files ahead in a bounded sliding window so memory stays small.
    """
    dos_time, dos_date = _dos_datetime()
    entries: list[_CentralEntry] = []
    offset = 0

    async def _fetch(arcname: str, key: str) -> tuple[str, bytes | None]:
        try:
            return arcname, await objects.get_bytes(key)
        except Exception as e:
            logger.warning(f"[zip] failed to read {key}: {e}")
            return arcname, None

    it = iter(items)
    pending: deque[asyncio.Task[tuple[str, bytes | None]]] = deque()

    def _fill() -> None:
        while len(pending) < concurrency:
            try:
                arcname, key = next(it)
            except StopIteration:
                break
            pending.append(asyncio.create_task(_fetch(arcname, key)))

    try:
        _fill()
        while pending:
            task = pending.popleft()
            _fill()
            arcname, data = await task
            if data is None:
                continue
            name_bytes = arcname.encode("utf-8")
            crc32 = zlib.crc32(data) & 0xFFFFFFFF
            size = len(data)
            header = _local_header(name_bytes, dos_time, dos_date, crc32, size)
            entries.append(
                _CentralEntry(
                    name_bytes=name_bytes,
                    dos_time=dos_time,
                    dos_date=dos_date,
                    crc32=crc32,
                    size=size,
                    header_offset=offset,
                )
            )
            offset += len(header) + size
            yield header
            yield data
    finally:
        for t in pending:
            t.cancel()

    cd_offset = offset
    cd_chunks = [_central_header(e) for e in entries]
    cd_bytes = b"".join(cd_chunks)
    eocd_bytes = _end_of_central_dir(len(entries), len(cd_bytes), cd_offset)
    yield cd_bytes + eocd_bytes


def zip_headers(download_name: str, ascii_fallback: str = "archive.zip") -> dict[str, str]:
    safe_ascii = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name).strip("_") or ascii_fallback
    if not safe_ascii.endswith(".zip"):
        safe_ascii += ".zip"
    return {
        "Content-Disposition": f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(download_name)}",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
