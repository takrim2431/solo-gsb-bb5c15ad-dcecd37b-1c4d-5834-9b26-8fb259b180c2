"""审计取证：按 设备编号 + 带时区半开测量时间区间 + 已提交版本 生成追溯包。

语义保证：
- 包只收录在 ``at_version`` 之前（含）已提交、且测量时刻落在
  ``[time_from, time_to)`` 的该设备读数；结论一律按固定 ``at_version`` 评估，
  因此生成之后发生的新提交、证书纠正或撤销永远不会改变既有包。
- 生成过程在单条 ``BEGIN IMMEDIATE`` 事务内完成快照：建包期间任何写操作
  （提交 / 纠正 / 撤销）都被写锁阻塞，不会混入包。
- 相同筛选条件与版本唯一对应一个包（幂等复用），内容与摘要必然一致。
- 完整性摘要基于确定性的规范化 JSON + SHA-256，可由审计方独立复算。
- 追溯包是审计派生物，不占用 versions 表的全局领域版本号。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from .config import get_settings
from .database import get_conn, write_tx
from .errors import ApiError
from .models import to_utc_string
from .service import _cert_snapshot, current_version, evaluate_reading, utc_now_string

# 规范化规则版本：摘要算法或规范化约定变更时递增
CANONICALIZATION_VERSION = "1"


def canonical_json(obj: Any) -> str:
    """确定性序列化：RFC 8259 JSON，紧凑分隔符，键按 Unicode 码点排序，
    非 ASCII 字符不转义；浮点采用 Python json 的最短可往返十进制表示。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonicalization_descriptor() -> Dict[str, Any]:
    """摘要规范化规则的机器可读说明，原样进入规范化清单。"""
    return {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "digest_algorithm": "SHA-256",
        "digest_encoding": "hexadecimal lowercase",
        "json": "RFC 8259 JSON; compact separators (',', ':'); UTF-8; ensure_ascii=false",
        "key_order": "object keys sorted by Unicode code point (json.dumps sort_keys=true)",
        "float": "ECMAScript-style JSON number via Python json.dumps shortest round-trip"
                 " representation; NaN/Infinity not permitted",
        "time_format": "UTC fixed width, YYYY-MM-DDTHH:MM:SS.ffffffZ",
        "entry_order": [["measured_at", "asc"], ["reading_no", "asc"]],
        "candidate_cert_order": [["certificate_no", "asc"], ["version_seq", "asc"]],
        "segment_digest": "sha256(concatenation of canonical entry_json bytes of the"
                          " segment in seq order) lowercase hex",
        "entries_digest": "sha256(concatenation of canonical entry_json bytes of all"
                          " entries in seq order) lowercase hex",
        "package_digest": "sha256(UTF-8 bytes of the canonical manifest JSON)"
                          " lowercase hex",
    }


class _PackageExists(Exception):
    """事务内信号：相同筛选条件+版本的包已被（并发）创建，走复用路径。"""


def _entry_canonical(
    conn: sqlite3.Connection, row: sqlite3.Row, at_version: int
) -> Dict[str, Any]:
    """冻结一条读数：固定版本视角下的结论与全部候选证书快照。"""
    conclusion = evaluate_reading(
        conn, row["device_no"], row["measured_at"], row["value"], at_version
    )
    return {
        "reading_no": row["reading_no"],
        "device_no": row["device_no"],
        "measured_at": row["measured_at"],
        "value": row["value"],
        "submitted_at_version": row["submitted_at_version"],
        "at_version": at_version,
        "conclusion": conclusion["status"],
        "reliance": conclusion["reliance"],
        "candidates": [_cert_snapshot(c) for c in conclusion["candidates"]],
    }


def _build_and_store(
    conn: sqlite3.Connection,
    package_id: str,
    device_no: str,
    time_from: str,
    time_to: str,
    at_version: int,
    page_size: int,
) -> Dict[str, Any]:
    """调用方已持有写事务。完成选数、冻结、分段、摘要与落库。"""
    rows = conn.execute(
        "SELECT * FROM readings"
        " WHERE device_no=? AND measured_at>=? AND measured_at<?"
        " AND submitted_at_version<=?"
        " ORDER BY measured_at, reading_no",
        (device_no, time_from, time_to, at_version),
    ).fetchall()

    entry_blobs: List[bytes] = []
    for row in rows:
        entry = _entry_canonical(conn, row, at_version)
        entry_blobs.append(canonical_json(entry).encode("utf-8"))

    total_count = len(entry_blobs)
    segment_count = (total_count + page_size - 1) // page_size
    segments: List[Dict[str, Any]] = []
    for index in range(segment_count):
        start = index * page_size
        chunk = entry_blobs[start:start + page_size]
        segments.append({
            "index": index,
            "offset": start,
            "count": len(chunk),
            "segment_digest": _sha256_hex(b"".join(chunk)),
        })

    entries_digest = _sha256_hex(b"".join(entry_blobs))
    manifest_canonical = {
        "criteria": {
            "device_no": device_no,
            "time_from": time_from,
            "time_to": time_to,
            "measured_at_interval": "[time_from, time_to)",
            "only_readings_committed_by": "submitted_at_version <= at_version",
        },
        "at_version": at_version,
        "page_size": page_size,
        "total_count": total_count,
        "entries_digest": entries_digest,
        "segments": segments,
        "canonicalization": _canonicalization_descriptor(),
    }
    manifest_json = canonical_json(manifest_canonical)
    package_digest = _sha256_hex(manifest_json.encode("utf-8"))
    created_at = utc_now_string()

    # 先落主档：同条件+版本的唯一约束在此处触发，不会写入任何条目
    conn.execute(
        "INSERT INTO audit_packages (package_id, device_no, time_from, time_to,"
        " at_version, page_size, total_count, entries_digest, package_digest,"
        " canonical_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (package_id, device_no, time_from, time_to, at_version, page_size,
         total_count, entries_digest, package_digest, manifest_json, created_at),
    )
    # 再冻结条目（外键引用已存在的主档）
    for seq, blob in enumerate(entry_blobs):
        conn.execute(
            "INSERT INTO audit_package_entries (package_id, seq, entry_json)"
            " VALUES (?,?,?)",
            (package_id, seq, blob.decode("utf-8")),
        )
    return {"manifest_canonical": manifest_canonical,
            "package_digest": package_digest, "created_at": created_at}


def create_audit_package(data: Dict[str, Any]) -> Dict[str, Any]:
    device_no = data["device_no"]
    time_from = to_utc_string(data["time_from"])
    time_to = to_utc_string(data["time_to"])

    while True:
        package_id = "ap-" + uuid.uuid4().hex
        try:
            with write_tx() as conn:
                latest = current_version(conn)
                if not 1 <= data["at_version"] <= latest:
                    raise ApiError(
                        422, "invalid_version",
                        f"at_version must be within [1, {latest}]",
                    )
                max_page = get_settings().max_page_size
                page_size = data["page_size"] or max_page
                if page_size > max_page:
                    raise ApiError(
                        422, "invalid_page_size",
                        f"page_size must not exceed {max_page}",
                    )
                try:
                    built = _build_and_store(
                        conn, package_id, device_no, time_from, time_to,
                        data["at_version"], page_size,
                    )
                except sqlite3.IntegrityError as e:
                    # 同条件+版本唯一约束：并发下对方先建，整体回滚后复用
                    raise _PackageExists() from e
                out = dict(built["manifest_canonical"])
                out["package_id"] = package_id
                out["package_digest"] = built["package_digest"]
                out["created_at"] = built["created_at"]
                out["reused"] = False
                out["segment_count"] = len(built["manifest_canonical"]["segments"])
                return out
        except _PackageExists:
            existing = find_audit_package(
                device_no, time_from, time_to, data["at_version"]
            )
            # 对方事务理论上已提交；极端情况下重试
            if existing is not None:
                existing["reused"] = True
                return existing


def _pkg_row(conn: sqlite3.Connection, package_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_packages WHERE package_id=?", (package_id,)
    ).fetchone()


def _manifest_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    manifest = json.loads(row["canonical_json"])
    manifest["package_id"] = row["package_id"]
    manifest["package_digest"] = row["package_digest"]
    manifest["created_at"] = row["created_at"]
    manifest["segment_count"] = len(manifest["segments"])
    return manifest


def find_audit_package(
    device_no: str, time_from: str, time_to: str, at_version: int
) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM audit_packages WHERE device_no=? AND time_from=?"
            " AND time_to=? AND at_version=?",
            (device_no, time_from, time_to, at_version),
        ).fetchone()
        return _manifest_from_row(row) if row is not None else None


def get_audit_package(conn: sqlite3.Connection, package_id: str) -> Optional[Dict[str, Any]]:
    row = _pkg_row(conn, package_id)
    return _manifest_from_row(row) if row is not None else None


def get_audit_segment(
    conn: sqlite3.Connection, package_id: str, segment_index: int
) -> Optional[Dict[str, Any]]:
    row = _pkg_row(conn, package_id)
    if row is None:
        return None
    page_size = int(row["page_size"])
    total_count = int(row["total_count"])
    segment_count = (total_count + page_size - 1) // page_size
    if segment_index < 0 or segment_index >= segment_count:
        raise ApiError(
            404, "unknown_segment",
            f"segment {segment_index} not found; package has {segment_count}"
            f" segment(s) numbered 0..{max(segment_count - 1, 0)}"
            + (" (empty package has no segments)" if segment_count == 0 else ""),
        )
    offset = segment_index * page_size
    entries = [
        json.loads(r["entry_json"])
        for r in conn.execute(
            "SELECT entry_json FROM audit_package_entries"
            " WHERE package_id=? AND seq>=? AND seq<? ORDER BY seq",
            (package_id, offset, min(offset + page_size, total_count)),
        ).fetchall()
    ]
    digest = row["entries_digest"]
    seg_digest = next(
        s["segment_digest"]
        for s in json.loads(row["canonical_json"])["segments"]
        if s["index"] == segment_index
    )
    next_offset = offset + len(entries)
    return {
        "package_id": package_id,
        "segment_index": segment_index,
        "segment_count": segment_count,
        "page_size": page_size,
        "offset": offset,
        "count": len(entries),
        "next_offset": next_offset if next_offset < total_count else None,
        "has_more": next_offset < total_count,
        "segment_digest": seg_digest,
        "entries_digest": digest,
        "entries": entries,
    }
