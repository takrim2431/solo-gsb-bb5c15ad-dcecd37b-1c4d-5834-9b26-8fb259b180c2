"""领域服务：证书生命周期、读数可信性评估、单调版本与幂等。

结论状态：
- unverified  未验证：测量时刻没有任何有效证书覆盖
- review      待复核：多份有效证书同时覆盖
- conforming  合格：唯一匹配且数值落在闭区间允许值内
- out_of_range 超限：唯一匹配但数值超出允许值
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .database import get_conn, write_tx
from .errors import ApiError
from .models import UTC_FMT, to_utc_string

CONCLUSIONS = ("unverified", "review", "conforming", "out_of_range")


# ---------------------------------------------------------------- 基础工具

def utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def _dt(value: Any) -> str:
    """请求中的 datetime 归一化为 UTC 定长字符串。"""
    return to_utc_string(value)


def canonical_json(payload: Dict[str, Any]) -> str:
    """幂等指纹：对归一化后的内容做稳定序列化（键排序、无空白）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cert_payload_dict(
    certificate_no: str,
    device_no: str,
    valid_from: str,
    valid_to: str,
    min_value: float,
    max_value: float,
    **extra: Any,
) -> Dict[str, Any]:
    p = {
        "certificate_no": certificate_no,
        "device_no": device_no,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "min_value": min_value,
        "max_value": max_value,
    }
    p.update(extra)
    return p


def _cert_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "certificate_no": row["certificate_no"],
        "version_seq": row["version_seq"],
        "device_no": row["device_no"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
        "created_at_version": row["created_at_version"],
        "superseded_at_version": row["superseded_at_version"],
    }


# ---------------------------------------------------------------- 版本查询

def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM versions").fetchone()
    return int(row["v"])


def _resolve_at_version(conn: sqlite3.Connection, at_version: Optional[int]) -> int:
    latest = current_version(conn)
    if at_version is None:
        return latest
    if at_version < 1 or at_version > latest:
        raise ApiError(422, "invalid_version", f"at_version must be within [1, {latest}]")
    return at_version


def _insert_version(
    conn: sqlite3.Connection,
    event_type: str,
    request_id: str,
    payload: Dict[str, Any],
) -> int:
    """追加一条版本流水。request_id 唯一冲突 -> 幂等复用/冲突报错。

    调用方必须已经在写事务内完成所有业务校验。
    """
    body = canonical_json(payload)
    try:
        cur = conn.execute(
            "INSERT INTO versions (event_type, request_id, request_payload_json,"
            " fingerprint, response_json, created_at) VALUES (?,?,?,?,NULL,?)",
            (event_type, request_id, body, body, utc_now_string()),
        )
    except sqlite3.IntegrityError as e:  # request_id 复用
        row = conn.execute(
            "SELECT fingerprint, event_type FROM versions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is not None and row["fingerprint"] != body:
            raise ApiError(
                409,
                "request_id_conflict",
                f"request_id {request_id!r} was already used with different content",
            )
        # 理论上并发下另一个事务已提交相同内容；标记为重放
        raise _Replay(row["event_type"] if row else event_type) from e
    return int(cur.lastrowid)


class _Replay(Exception):
    """事务内信号：相同 request_id+内容已存在，走重放路径。"""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type


def _replay_response(request_id: str) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT version, event_type, response_json, created_at FROM versions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:  # 极端并发：对方事务已回滚
            raise ApiError(409, "request_id_conflict", "request_id is being processed")
        if not row["response_json"]:
            raise ApiError(409, "request_inflight", "request is being processed")
        resp = json.loads(row["response_json"])
        resp["idempotent_replay"] = True
        return resp


def _save_response(conn: sqlite3.Connection, version: int, response: Dict[str, Any]) -> None:
    conn.execute("UPDATE versions SET response_json=? WHERE version=?", (json.dumps(response, ensure_ascii=False), version))


# ---------------------------------------------------------------- 证书查询

def _cert_version_row(conn: sqlite3.Connection, certificate_no: str, seq: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_versions WHERE certificate_no=? AND version_seq=?",
        (certificate_no, seq),
    ).fetchone()


def _cert_master(conn: sqlite3.Connection, certificate_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE certificate_no=?", (certificate_no,)
    ).fetchone()


def _active_cert_rows(
    conn: sqlite3.Connection, device_no: str, at_version: int, measured_at: str
) -> List[sqlite3.Row]:
    """在指定版本视角下，覆盖测量时刻的、当前仍有效的证书版本。

    有效条件（全部满足）：
    - created_at_version <= at_version                 （该版本已经可见）
    - superseded_at_version IS NULL 或 > at_version   （未被纠正/撤销失效）
    - valid_from <= measured_at < valid_to            （左闭右开有效期）
    """
    return list(conn.execute(
        """
        SELECT cv.*
        FROM certificate_versions cv
        JOIN versions v ON v.version = cv.created_at_version
        WHERE cv.device_no = ?
          AND cv.created_at_version <= ?
          AND (cv.superseded_at_version IS NULL OR cv.superseded_at_version > ?)
          AND cv.valid_from <= ?
          AND cv.valid_to   >  ?
        ORDER BY cv.certificate_no, cv.version_seq
        """,
        (device_no, at_version, at_version, measured_at, measured_at),
    ).fetchall())


def get_certificate(
    conn: sqlite3.Connection, certificate_no: str, at_version: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    master = _cert_master(conn, certificate_no)
    if master is None:
        return None
    av = _resolve_at_version(conn, at_version)
    if master["created_at_version"] > av:
        return None
    # 在该版本视角下的最新内容版本序号
    row = conn.execute(
        """
        SELECT * FROM certificate_versions
        WHERE certificate_no=? AND created_at_version <= ?
        ORDER BY version_seq DESC LIMIT 1
        """,
        (certificate_no, av),
    ).fetchone()
    out = _cert_snapshot(row)
    revoked = bool(master["is_revoked"]) and master["revoked_at_version"] <= av
    if revoked:
        out["status"] = "revoked"
    elif row["superseded_at_version"] is not None and row["superseded_at_version"] <= av:
        # 正常不会落到这：下一条内容版本应已可见
        out["status"] = "superseded"
    else:
        out["status"] = "active"
    out["revoked_reason"] = master["revoked_reason"]
    out["revoked_at_version"] = master["revoked_at_version"] if revoked else None
    out["at_version"] = av
    return out


def list_certificates(
    conn: sqlite3.Connection,
    at_version: Optional[int],
    device_no: Optional[str],
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    av = _resolve_at_version(conn, at_version)
    sql = "SELECT certificate_no FROM certificates WHERE created_at_version <= ?"
    params: List[Any] = [av]
    if device_no:
        sql += " AND certificate_no IN (SELECT DISTINCT certificate_no FROM certificate_versions WHERE device_no=?)"
        params.append(device_no)
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM (" + sql + ")", params
    ).fetchone()["c"]
    sql += " ORDER BY certificate_no LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    items = [get_certificate(conn, r["certificate_no"], av) for r in rows]
    return {"at_version": av, "total": total, "limit": limit, "offset": offset, "items": items}


def certificate_history(conn: sqlite3.Connection, certificate_no: str) -> Dict[str, Any]:
    master = _cert_master(conn, certificate_no)
    if master is None:
        raise ApiError(404, "unknown_certificate", f"certificate {certificate_no!r} not found")
    rows = conn.execute(
        "SELECT * FROM certificate_versions WHERE certificate_no=? ORDER BY version_seq",
        (certificate_no,),
    ).fetchall()
    return {
        "certificate_no": certificate_no,
        "current_version_seq": master["current_version_seq"],
        "is_revoked": bool(master["is_revoked"]),
        "revoked_at_version": master["revoked_at_version"],
        "revoked_reason": master["revoked_reason"],
        "versions": [_cert_snapshot(r) for r in rows],
    }


# ---------------------------------------------------------------- 读数评估

def evaluate_reading(
    conn: sqlite3.Connection,
    device_no: str,
    measured_at: str,
    value: float,
    at_version: int,
) -> Dict[str, Any]:
    """在指定版本视角下评估单条读数（不依赖读数是否已入库）。"""
    matches = _active_cert_rows(conn, device_no, at_version, measured_at)
    if not matches:
        return {"status": "unverified", "reliance": None, "candidates": []}
    if len(matches) > 1:
        return {
            "status": "review",
            "reliance": None,
            "candidates": [_cert_snapshot(m) for m in matches],
        }
    cert = matches[0]
    status = "conforming" if cert["min_value"] <= value <= cert["max_value"] else "out_of_range"
    return {"status": status, "reliance": _cert_snapshot(cert), "candidates": [_cert_snapshot(cert)]}


def _evaluate_affected(
    conn: sqlite3.Connection, before_version: int, new_version: int, device_nos: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """重算在 before_version 之前（含）提交、且设备相关的所有读数的结论变化。"""
    if not device_nos:
        return []
    placeholders = ",".join("?" for _ in device_nos)
    rows = conn.execute(
        f"SELECT * FROM readings WHERE submitted_at_version <= ? AND device_no IN ({placeholders})"
        " ORDER BY reading_no",
        (before_version, *device_nos),
    ).fetchall()
    affected: List[Dict[str, Any]] = []
    for r in rows:
        before = evaluate_reading(conn, r["device_no"], r["measured_at"], r["value"], before_version)
        after = evaluate_reading(conn, r["device_no"], r["measured_at"], r["value"], new_version)
        affected.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": r["measured_at"],
            "value": r["value"],
            "before": {"at_version": before_version, **before},
            "after": {"at_version": new_version, **after},
            "conclusion_changed": before["status"] != after["status"],
        })
    return affected


# ---------------------------------------------------------------- 写操作：签发

def issue_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _cert_payload_dict(
        data["certificate_no"],
        data["device_no"],
        _dt(data["valid_from"]),
        _dt(data["valid_to"]),
        float(data["min_value"]),
        float(data["max_value"]),
    )
    request_id = data["request_id"]

    while True:
        try:
            with write_tx() as conn:
                # 先登记版本（含幂等判定），再做业务校验并写入；
                # 失败整体回滚，不留下版本或部分数据
                version = _insert_version(conn, "cert_issue", request_id, payload)
                if _cert_master(conn, payload["certificate_no"]) is not None:
                    raise ApiError(
                        409,
                        "certificate_exists",
                        f"certificate {payload['certificate_no']!r} already exists;"
                        " use correct or revoke instead",
                    )
                conn.execute(
                    "INSERT INTO certificates (certificate_no, current_version_seq,"
                    " is_revoked, created_at_version, updated_at_version)"
                    " VALUES (?,1,0,?,?)",
                    (payload["certificate_no"], version, version),
                )
                conn.execute(
                    "INSERT INTO certificate_versions (certificate_no, version_seq,"
                    " device_no, valid_from, valid_to, min_value, max_value, created_at_version)"
                    " VALUES (?,1,?,?,?,?,?,?)",
                    (
                        payload["certificate_no"], payload["device_no"],
                        payload["valid_from"], payload["valid_to"],
                        payload["min_value"], payload["max_value"], version,
                    ),
                )
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_issue",
                    "idempotent_replay": False,
                    "certificate": _cert_snapshot(_cert_version_row(conn, payload["certificate_no"], 1)),
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：纠正

def correct_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _cert_payload_dict(
        data["certificate_no"],
        data["device_no"],
        _dt(data["valid_from"]),
        _dt(data["valid_to"]),
        float(data["min_value"]),
        float(data["max_value"]),
        reason=data.get("reason"),
    )
    request_id = data["request_id"]

    while True:
        try:
            with write_tx() as conn:
                before_version = current_version(conn)
                # 先登记版本（幂等），再校验目标与执行纠正，失败整体回滚
                version = _insert_version(conn, "cert_correct", request_id, payload)
                master = _cert_master(conn, payload["certificate_no"])
                if master is None:
                    raise ApiError(404, "unknown_certificate",
                                   f"certificate {payload['certificate_no']!r} not found; issue it first")
                if master["is_revoked"]:
                    raise ApiError(409, "certificate_revoked",
                                   "a revoked certificate cannot be corrected")
                old_seq = int(master["current_version_seq"])
                old_row = _cert_version_row(conn, payload["certificate_no"], old_seq)
                new_seq = old_seq + 1

                # 旧版本在新版本起失效（历史行不被覆盖，仅登记失效版本）
                conn.execute(
                    "UPDATE certificate_versions SET superseded_at_version=? WHERE id=?",
                    (version, old_row["id"]),
                )
                conn.execute(
                    "INSERT INTO certificate_versions (certificate_no, version_seq,"
                    " device_no, valid_from, valid_to, min_value, max_value, created_at_version)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        payload["certificate_no"], new_seq, payload["device_no"],
                        payload["valid_from"], payload["valid_to"],
                        payload["min_value"], payload["max_value"], version,
                    ),
                )
                conn.execute(
                    "UPDATE certificates SET current_version_seq=?, updated_at_version=?"
                    " WHERE certificate_no=?",
                    (new_seq, version, payload["certificate_no"]),
                )

                devices = {old_row["device_no"], payload["device_no"]}
                affected = _evaluate_affected(conn, before_version, version, tuple(sorted(devices)))
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_correct",
                    "idempotent_replay": False,
                    "certificate_no": payload["certificate_no"],
                    "superseded_version_seq": old_seq,
                    "certificate": _cert_snapshot(_cert_version_row(conn, payload["certificate_no"], new_seq)),
                    "affected_readings": affected,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：撤销

def revoke_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    request_id = data["request_id"]
    payload = {
        "certificate_no": data["certificate_no"],
        "reason": data.get("reason"),
    }
    while True:
        try:
            with write_tx() as conn:
                before_version = current_version(conn)
                version = _insert_version(conn, "cert_revoke", request_id, payload)
                master = _cert_master(conn, payload["certificate_no"])
                if master is None:
                    raise ApiError(404, "unknown_certificate",
                                   f"certificate {payload['certificate_no']!r} not found")
                if master["is_revoked"]:
                    raise ApiError(409, "certificate_revoked",
                                   "certificate is already revoked")
                cur_seq = int(master["current_version_seq"])
                cur_row = _cert_version_row(conn, payload["certificate_no"], cur_seq)

                conn.execute(
                    "UPDATE certificate_versions SET superseded_at_version=? WHERE id=?",
                    (version, cur_row["id"]),
                )
                conn.execute(
                    "UPDATE certificates SET is_revoked=1, revoked_at_version=?,"
                    " revoked_reason=?, updated_at_version=? WHERE certificate_no=?",
                    (version, payload["reason"], version, payload["certificate_no"]),
                )
                affected = _evaluate_affected(conn, before_version, version, (cur_row["device_no"],))
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_revoke",
                    "idempotent_replay": False,
                    "certificate_no": payload["certificate_no"],
                    "revoked_version_seq": cur_seq,
                    "reason": payload["reason"],
                    "affected_readings": affected,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：读数提交

def submit_readings(data: Dict[str, Any]) -> Dict[str, Any]:
    request_id = data["request_id"]
    items: List[Dict[str, Any]] = []
    for r in data["readings"]:
        items.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": _dt(r["measured_at"]),
            "value": float(r["value"]),
        })
    payload = {"readings": items}

    while True:
        try:
            with write_tx() as conn:
                version = _insert_version(conn, "reading_submit", request_id, payload)
                # 未知目标/重复读数：全部先校验，保证不留下部分变更
                existing = {
                    row["reading_no"]
                    for row in conn.execute(
                        "SELECT reading_no FROM readings WHERE reading_no IN (%s)"
                        % ",".join("?" for _ in items),
                        [i["reading_no"] for i in items],
                    ).fetchall()
                }
                if existing:
                    raise ApiError(
                        409,
                        "reading_exists",
                        f"readings already exist: {sorted(existing)}; readings are immutable",
                    )
                now = utc_now_string()
                for i in items:
                    conn.execute(
                        "INSERT INTO readings (reading_no, device_no, measured_at, value,"
                        " submitted_at_version, created_at) VALUES (?,?,?,?,?,?)",
                        (i["reading_no"], i["device_no"], i["measured_at"],
                         i["value"], version, now),
                    )
                results = []
                for i in items:
                    conclusion = evaluate_reading(
                        conn, i["device_no"], i["measured_at"], i["value"], version
                    )
                    results.append({
                        "reading_no": i["reading_no"],
                        "submitted_at_version": version,
                        **conclusion,
                    })
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "reading_submit",
                    "idempotent_replay": False,
                    "results": results,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 读接口

def get_reading(
    conn: sqlite3.Connection, reading_no: str, at_version: Optional[int]
) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM readings WHERE reading_no=?", (reading_no,)).fetchone()
    if row is None:
        return None
    av = _resolve_at_version(conn, at_version)
    if av < row["submitted_at_version"]:
        raise ApiError(
            422,
            "invalid_version",
            f"reading was submitted at version {row['submitted_at_version']},"
            f" cannot evaluate at earlier version {av}",
        )
    conclusion = evaluate_reading(conn, row["device_no"], row["measured_at"], row["value"], av)
    return {
        "reading_no": row["reading_no"],
        "device_no": row["device_no"],
        "measured_at": row["measured_at"],
        "value": row["value"],
        "submitted_at_version": row["submitted_at_version"],
        "at_version": av,
        **conclusion,
    }


def list_readings(
    conn: sqlite3.Connection,
    at_version: Optional[int],
    device_no: Optional[str],
    status_filter: Optional[str],
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    av = _resolve_at_version(conn, at_version)
    if status_filter is not None and status_filter not in CONCLUSIONS:
        raise ApiError(422, "invalid_status",
                       f"status must be one of {list(CONCLUSIONS)}")
    rows = conn.execute(
        "SELECT * FROM readings WHERE submitted_at_version <= ?"
        + (" AND device_no=?" if device_no else "")
        + " ORDER BY measured_at, reading_no LIMIT ? OFFSET ?",
        ([av] + ([device_no] if device_no else []) + [limit + 1, offset]),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        c = evaluate_reading(conn, r["device_no"], r["measured_at"], r["value"], av)
        if status_filter and c["status"] != status_filter:
            continue
        items.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": r["measured_at"],
            "value": r["value"],
            "submitted_at_version": r["submitted_at_version"],
            "at_version": av,
            **c,
        })
    return {
        "at_version": av,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "items": items,
    }


def list_versions(conn: sqlite3.Connection, limit: int, offset: int) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]
    rows = conn.execute(
        "SELECT version, event_type, request_id, request_payload_json, created_at"
        " FROM versions ORDER BY version DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "version": r["version"],
                "event_type": r["event_type"],
                "request_id": r["request_id"],
                "request_payload": json.loads(r["request_payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM versions WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "request_id": row["request_id"],
        "version": row["version"],
        "event_type": row["event_type"],
        "request_payload": json.loads(row["request_payload_json"]),
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "created_at": row["created_at"],
    }
