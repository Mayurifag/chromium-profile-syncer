from __future__ import annotations

import logging
import struct
from pathlib import Path

_LOG = logging.getLogger(__name__)

_CRC32C_TABLE: list[int] | None = None


def _crc32c(data: bytes, crc: int = 0) -> int:
    global _CRC32C_TABLE
    if _CRC32C_TABLE is None:
        table = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
            table.append(c)
        _CRC32C_TABLE = table
    crc ^= 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


def _mask_crc(crc: int) -> int:
    return ((((crc >> 15) | (crc << 17)) & 0xFFFFFFFF) + 0xA282EAD8) & 0xFFFFFFFF


def _varint_decode(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _varint_encode(n: int) -> bytes:
    out = []
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _snappy_decompress(data: bytes) -> bytes:
    try:
        import cramjam

        return bytes(cramjam.snappy.decompress_raw(data))
    except Exception:
        return data


def _read_block(file_data: bytes, offset: int, size: int) -> bytes:
    raw = file_data[offset : offset + size]
    compression = file_data[offset + size] if offset + size < len(file_data) else 0
    if compression == 1:
        raw = _snappy_decompress(raw)
    num_restarts = struct.unpack_from("<I", raw, len(raw) - 4)[0]
    return raw[: len(raw) - 4 - num_restarts * 4]


def _iter_block_kv(block: bytes):
    pos = 0
    cur_key = b""
    while pos < len(block):
        shared, pos = _varint_decode(block, pos)
        unshared, pos = _varint_decode(block, pos)
        vlen, pos = _varint_decode(block, pos)
        cur_key = cur_key[:shared] + block[pos : pos + unshared]
        pos += unshared
        val = block[pos : pos + vlen]
        pos += vlen
        yield cur_key[:-8] if len(cur_key) >= 8 else cur_key, val


def _read_ldb(path: Path) -> dict[bytes, bytes]:
    data = path.read_bytes()
    if len(data) < 48:
        return {}
    footer = data[-48:]
    p = 0
    _, p = _varint_decode(footer, p)
    _, p = _varint_decode(footer, p)
    idx_offset, p = _varint_decode(footer, p)
    idx_size, _ = _varint_decode(footer, p)
    iblock = _read_block(data, idx_offset, idx_size)
    result: dict[bytes, bytes] = {}
    for _, ival in _iter_block_kv(iblock):
        try:
            boffset, p2 = _varint_decode(ival, 0)
            bsize, _ = _varint_decode(ival, p2)
            for k, v in _iter_block_kv(_read_block(data, boffset, bsize)):
                result[k] = v
        except Exception:
            continue
    return result


def _read_log(path: Path) -> list[tuple[bytes, bytes | None]]:
    data = path.read_bytes()
    BLOCK = 32768
    updates: list[tuple[bytes, bytes | None]] = []
    pos = 0
    while pos + 7 <= len(data):
        blk_start = (pos // BLOCK) * BLOCK
        if pos - blk_start + 7 > BLOCK:
            pos = blk_start + BLOCK
            continue
        length = struct.unpack_from("<H", data, pos + 4)[0]
        rtype = data[pos + 6]
        pos += 7
        if rtype == 0:
            pos = blk_start + BLOCK
            continue
        record = data[pos : pos + length]
        pos += length
        if len(record) < 12:
            continue
        count = struct.unpack_from("<I", record, 8)[0]
        bp = 12
        for _ in range(count):
            if bp >= len(record):
                break
            vtype = record[bp]
            bp += 1
            klen, bp = _varint_decode(record, bp)
            key = record[bp : bp + klen]
            bp += klen
            if vtype == 1:
                vlen, bp = _varint_decode(record, bp)
                updates.append((key, record[bp : bp + vlen]))
                bp += vlen
            else:
                updates.append((key, None))
    return updates


def read_all_kv(db_path: Path) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    for ldb in sorted(db_path.glob("*.ldb")):
        try:
            result.update(_read_ldb(ldb))
        except Exception as e:
            _LOG.warning("ldb_filter: skip %s: %s", ldb.name, e)
    for log in sorted(db_path.glob("*.log")):
        try:
            for k, v in _read_log(log):
                if v is None:
                    result.pop(k, None)
                else:
                    result[k] = v
        except Exception as e:
            _LOG.warning("ldb_filter: skip log %s: %s", log.name, e)
    return result


_LOG_BLOCK = 32768
_LOG_HEADER = 7


def _write_log_batches(batches: list[bytes]) -> bytes:
    # _read_log doesn't reassemble FIRST/MIDDLE/LAST fragments, so each batch
    # must fit in a single block as a FULL record. Pad to next block boundary
    # when the current block can't hold it. Caller must ensure batch payload
    # ≤ _LOG_BLOCK - _LOG_HEADER (~32 KiB).
    out = bytearray()
    for batch in batches:
        used = len(out) % _LOG_BLOCK
        space = _LOG_BLOCK - used - _LOG_HEADER
        if space < len(batch):
            out += bytes(_LOG_BLOCK - used)
        crc = _mask_crc(_crc32c(bytes([1]) + batch))
        out += struct.pack("<IHB", crc, len(batch), 1) + batch
    return bytes(out)


def _manifest_record(payload: bytes) -> bytes:
    crc = _mask_crc(_crc32c(b"\x01" + payload))
    return struct.pack("<IHB", crc, len(payload), 1) + payload


def write_minimal_db(dst: Path, kv: dict[bytes, bytes]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    batches = []
    for seq, (k, v) in enumerate(kv.items(), start=1):
        batch = struct.pack("<Q", seq) + struct.pack("<I", 1)
        batch += b"\x01" + _varint_encode(len(k)) + k + _varint_encode(len(v)) + v
        batches.append(batch)
    (dst / "000001.log").write_bytes(_write_log_batches(batches))
    comparator = b"leveldb.BytewiseComparator"
    edit = (
        _varint_encode(1)
        + _varint_encode(len(comparator))
        + comparator
        + _varint_encode(2)
        + _varint_encode(1)
        + _varint_encode(3)
        + _varint_encode(3)
        + _varint_encode(4)
        + _varint_encode(len(kv))
    )
    (dst / "MANIFEST-000001").write_bytes(_manifest_record(edit))
    (dst / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")


def copy_filtered(
    src: Path,
    dst: Path,
    skip_prefixes: tuple[bytes, ...],
) -> None:
    import shutil

    all_kv = read_all_kv(src)
    filtered = {
        k: v for k, v in all_kv.items() if not any(k.startswith(p) for p in skip_prefixes)
    }
    dropped_mb = (
        sum(len(v) for k, v in all_kv.items() if any(k.startswith(p) for p in skip_prefixes))
        / 1024
        / 1024
    )
    _LOG.info(
        "ldb_filter %s: %d->%d keys, dropped %.1fMB cache",
        src.name,
        len(all_kv),
        len(filtered),
        dropped_mb,
    )
    if dst.exists():
        shutil.rmtree(dst)
    write_minimal_db(dst, filtered)
