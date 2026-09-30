"""审计取证追溯包端到端测试。

覆盖：
- 只收录 at_version 之前（含）已提交、测量时刻落在半开区间的读数
- 每条读数固定版本视角下的结论与全部候选证书快照
- 无匹配读数 -> 空包（201，total_count=0，无分段）
- 筛选条件 / 版本 / 总条数 / 规范化说明齐全
- 摘要可按声明的规范化规则独立复算（测试端不引用实现代码）
- 分段拼接顺序稳定、无遗漏、无重复；段摘要与整包摘要自洽
- 同条件+版本幂等复用，内容与摘要完全一致
- 生成之后新提交 / 纠正 / 撤销不影响已生成的包
- 版本越界、无效时间区间明确报错且不产生半成品
- 未知包 / 越界分段 -> 404
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
import time
from datetime import datetime, timezone

import pytest

DB_PATH = "/tmp/trace_audit_test_%d.db" % os.getpid()
for suffix in ("", "-wal", "-shm"):
    p = pathlib.Path(DB_PATH + suffix)
    if p.exists():
        p.unlink()
os.environ["TRACE_DB_PATH"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reset_settings_cache  # noqa: E402
from app.main import app  # noqa: E402

API = "/api/v1"


# 测试端独立实现的规范化序列化：验证“可独立复算”，不复用服务端代码
def cj(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(scope="module")
def client():
    # 在 fixture 内再次固定，避免同进程其它测试模块在收集阶段覆盖环境变量
    os.environ["TRACE_DB_PATH"] = DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(DB_PATH + suffix)
        if p.exists():
            p.unlink()
    reset_settings_cache()
    with TestClient(app) as c:
        yield c


def _issue(client, req_id, cert_no, device, vf, vt, lo, hi):
    r = client.post(f"{API}/certificates", json={
        "request_id": req_id, "certificate_no": cert_no, "device_no": device,
        "valid_from": vf, "valid_to": vt, "min_value": lo, "max_value": hi,
    })
    assert r.status_code == 200, r.text
    return r.json()["version"]


def _submit(client, req_id, items):
    r = client.post(f"{API}/readings", json={"request_id": req_id, "readings": items})
    assert r.status_code == 200, r.text
    return r.json()["version"]


@pytest.fixture(scope="module")
def seeded(client):
    # v1 证书：DA 有效期 [2026-01-01, 2026-03-01)，允许 [10,20]
    _issue(client, "a-req-cert-1", "CA-1", "DA",
           "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z", 10.0, 20.0)
    # v2 提交读数：4 条区间内候选 + 1 条区间外（2026-03-01 右边界）
    _submit(client, "a-req-r1", [
        {"reading_no": "A1", "device_no": "DA",
         "measured_at": "2026-01-10T00:00:00Z", "value": 11.0},
        {"reading_no": "A2", "device_no": "DA",
         "measured_at": "2026-01-15T00:00:00Z", "value": 25.0},   # 超限
        {"reading_no": "A3", "device_no": "DA",
         "measured_at": "2026-02-01T12:00:00Z", "value": 15.0},
        {"reading_no": "A4", "device_no": "DA",
         "measured_at": "2026-02-20T08:30:00Z", "value": 20.0},   # 闭区间边界
        {"reading_no": "A5", "device_no": "DA",
         "measured_at": "2026-03-01T00:00:00Z", "value": 15.0},   # 右开不含
    ])
    # v3 其它设备读数：不混入 DA 的包
    _submit(client, "a-req-r2", [
        {"reading_no": "B1", "device_no": "DB",
         "measured_at": "2026-01-12T00:00:00Z", "value": 1.0},
    ])
    # v4 第二份证书覆盖 A1/A2/A3 -> review（多候选）
    _issue(client, "a-req-cert-2", "CA-2", "DA",
           "2026-01-05T00:00:00Z", "2026-02-10T00:00:00Z", 0.0, 100.0)
    # v5 纠正 CA-1：允许范围改为 [12,20]，A1 此后若唯一匹配会超限
    client.post(f"{API}/certificates/CA-1/correct", json={
        "request_id": "a-req-cert-1-fix", "certificate_no": "CA-1", "device_no": "DA",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-03-01T00:00:00Z",
        "min_value": 12.0, "max_value": 20.0, "reason": "tightened",
    })
    # v6 撤销 CA-2：A1/A2/A3 重新唯一匹配 CA-1 v2
    client.post(f"{API}/certificates/CA-2/revoke", json={
        "request_id": "a-req-cert-2-revoke", "certificate_no": "CA-2",
        "reason": "duplicate",
    })
    # v7 区间起点之前的读数（半开：等于 time_from 也会包含，这条更早）
    _submit(client, "a-req-r3", [
        {"reading_no": "A0", "device_no": "DA",
         "measured_at": "2025-12-31T23:59:59Z", "value": 15.0},
    ])
    # v8：稍后才提交的读数，固定在 v7 的包不应收录（版本截止）
    late_version = _submit(client, "a-req-r-late", [
        {"reading_no": "A9", "device_no": "DA",
         "measured_at": "2026-02-05T00:00:00Z", "value": 15.0},
    ])
    return {"late_version": late_version}


CRITERIA = {
    "device_no": "DA",
    "time_from": "2026-01-01T00:00:00Z",
    "time_to": "2026-03-01T00:00:00Z",
}


def _create(client, at_version=7, page_size=2, **over):
    body = {"at_version": at_version, "page_size": page_size, **CRITERIA, **over}
    r = client.post(f"{API}/audit/packages", json=body)
    return r


def test_01_package_filtering_and_snapshot(seeded, client):
    r = _create(client, at_version=7, page_size=2)
    assert r.status_code == 201, r.text
    pkg = r.json()
    assert pkg["reused"] is False
    # 只收录 A1..A4：A0 在起点前、A5 在右边界、A9 在 v8 才提交（晚于 v7）、B1 别的设备
    assert pkg["total_count"] == 4
    assert pkg["at_version"] == 7
    assert pkg["criteria"]["measured_at_interval"] == "[time_from, time_to)"
    assert pkg["page_size"] == 2
    assert pkg["segment_count"] == 2
    assert pkg["package_id"].startswith("ap-")
    # 规范化规则必须随包给出
    canon = pkg["canonicalization"]
    assert canon["canonicalization_version"] == "1"
    assert canon["digest_algorithm"] == "SHA-256"
    assert "entry_order" in canon and "package_digest" in canon
    assert pkg["created_at"]

    # 全局领域版本不因生成审计包而变化
    assert client.get(f"{API}/status").json()["current_version"] == seeded["late_version"]

    # 段 0/1 拼接
    entries = []
    for i in range(2):
        s = client.get(f"{API}/audit/packages/{pkg['package_id']}/segments/{i}")
        assert s.status_code == 200, s.text
        seg = s.json()
        assert seg["segment_index"] == i
        assert seg["segment_count"] == 2
        entries.extend(seg["entries"])
        if i == 0:
            assert seg["count"] == 2 and seg["has_more"] is True
            assert seg["next_offset"] == 2
        else:
            assert seg["count"] == 2 and seg["has_more"] is False
            assert seg["next_offset"] is None

    # 稳定顺序：measured_at 升序，无遗漏/重复
    assert [e["reading_no"] for e in entries] == ["A1", "A2", "A3", "A4"]
    seqs_keys = [(e["measured_at"], e["reading_no"]) for e in entries]
    assert seqs_keys == sorted(seqs_keys)
    assert len({e["reading_no"] for e in entries}) == 4

    by_no = {e["reading_no"]: e for e in entries}
    # v7 视角：CA-2 已撤销，唯一匹配 CA-1 v2，范围 [12,20]
    assert by_no["A1"]["conclusion"] == "out_of_range"          # 11.0 < 12
    assert by_no["A2"]["conclusion"] == "out_of_range"          # 25.0
    assert by_no["A3"]["conclusion"] == "conforming"
    assert by_no["A4"]["conclusion"] == "conforming"            # 闭区间上界 20
    for e in entries:
        assert e["at_version"] == 7
        assert e["submitted_at_version"] == 2
        assert e["reliance"]["certificate_no"] == "CA-1"
        assert e["reliance"]["version_seq"] == 2
        assert [c["certificate_no"] for c in e["candidates"]] == ["CA-1"]


def test_02_digests_independently_recomputable(seeded, client):
    r = _create(client, at_version=7, page_size=2)
    pkg = r.json()

    # 取回全部条目（条目原文即规范化 entry_json）
    entry_blobs, seg_digests = [], []
    for i in range(pkg["segment_count"]):
        seg = client.get(
            f"{API}/audit/packages/{pkg['package_id']}/segments/{i}"
        ).json()
        blob = b"".join(cj(e).encode("utf-8") for e in seg["entries"])
        entry_blobs.append(blob)
        seg_digests.append(sha(blob))
        # 段摘要可独立复算
        assert seg["segment_digest"] == seg_digests[-1]

    declared_seg = [s["segment_digest"] for s in pkg["segments"]]
    assert declared_seg == seg_digests

    # 整包条目摘要：全部条目按序拼接
    assert pkg["entries_digest"] == sha(b"".join(entry_blobs))

    # 包摘要：清单按规范化 JSON 序列化后取 SHA-256；复算时剔除非摘要输入字段
    manifest = {k: v for k, v in pkg.items()
                if k in {"criteria", "at_version", "page_size", "total_count",
                         "entries_digest", "segments", "canonicalization"}}
    assert pkg["package_digest"] == sha(cj(manifest).encode("utf-8"))


def test_03_snapshot_is_version_pinned_and_immutable_after_mutations(seeded, client):
    # v7 视角：A1 超限（CA-1 v2 范围 [12,20]，CA-2 已撤销）
    pkg_v7 = _create(client, at_version=7, page_size=10).json()

    # v4 视角：CA-2 已签发未撤销，CA-1 仍是 v1 -> A1 多候选 review、合格候选齐全
    r = _create(client, at_version=4, page_size=10)
    assert r.status_code == 201
    pkg_v4 = r.json()
    seg = client.get(f"{API}/audit/packages/{pkg_v4['package_id']}/segments/0").json()
    a1 = next(e for e in seg["entries"] if e["reading_no"] == "A1")
    assert a1["conclusion"] == "review"
    assert a1["reliance"] is None
    assert {c["certificate_no"] for c in a1["candidates"]} == {"CA-1", "CA-2"}
    a2 = next(e for e in seg["entries"] if e["reading_no"] == "A2")
    assert a2["candidates"][0]["certificate_no"] == "CA-1"
    assert a2["candidates"][0]["version_seq"] == 1

    # 生成之后再发生撤销：v7 包内容与摘要不变
    client.post(f"{API}/certificates/CA-1/revoke", json={
        "request_id": "a-req-cert-1-revoke-late", "certificate_no": "CA-1",
        "reason": "post-package",
    })
    # 新读数提交同样不影响旧包
    _submit(client, "a-req-r-after", [
        {"reading_no": "A10", "device_no": "DA",
         "measured_at": "2026-01-20T00:00:00Z", "value": 15.0},
    ])
    again = client.get(f"{API}/audit/packages/{pkg_v7['package_id']}").json()
    assert again["package_digest"] == pkg_v7["package_digest"]
    assert again["total_count"] == 4
    seg = client.get(f"{API}/audit/packages/{pkg_v7['package_id']}/segments/0").json()
    assert next(e for e in seg["entries"] if e["reading_no"] == "A1")["conclusion"] == "out_of_range"

    # 当前读数结论已经改变，证明旧包确实冻结在 v7
    cur = client.get(f"{API}/readings/A1").json()
    assert cur["status"] == "unverified"

    # 两个不同视角的包摘要必然不同，且各自自洽
    assert pkg_v4["package_digest"] != pkg_v7["package_digest"]


def test_04_idempotent_same_criteria_and_version(seeded, client):
    first = _create(client, at_version=7, page_size=2)
    assert first.status_code == 201
    a = first.json()

    # 相同条件与版本（即便换 page_size）复用同一包 -> 内容与摘要完全一致
    second = _create(client, at_version=7, page_size=50)
    assert second.status_code == 201
    b = second.json()
    assert b["reused"] is True
    assert b["package_id"] == a["package_id"]
    assert b["package_digest"] == a["package_digest"]
    assert b["entries_digest"] == a["entries_digest"]
    assert b["page_size"] == 2  # 以首次创建为准

    # find 按条件定位
    r = client.get(f"{API}/audit/packages/find", params=CRITERIA | {"at_version": 7})
    assert r.status_code == 200
    assert r.json()["package_id"] == a["package_id"]


def test_05_empty_package(seeded, client):
    # 设备不存在 / 区间内无读数 -> 空包（明确返回，不是错误）
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "NO-DEV",
        "time_from": "2026-01-01T00:00:00Z",
        "time_to": "2026-02-01T00:00:00Z",
        "at_version": 7, "page_size": 10,
    })
    assert r.status_code == 201, r.text
    pkg = r.json()
    assert pkg["total_count"] == 0
    assert pkg["segments"] == []
    assert pkg["segment_count"] == 0
    assert pkg["entries_digest"] == sha(b"")
    # 空包包摘要仍可独立复算
    manifest = {k: v for k, v in pkg.items()
                if k in {"criteria", "at_version", "page_size", "total_count",
                         "entries_digest", "segments", "canonicalization"}}
    assert pkg["package_digest"] == sha(cj(manifest).encode("utf-8"))
    # 空包不存在第 0 段
    r = client.get(f"{API}/audit/packages/{pkg['package_id']}/segments/0")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_segment"

    # 区间夹在两条读数之间也是空包
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "DA",
        "time_from": "2026-01-11T00:00:00Z",
        "time_to": "2026-01-15T00:00:00Z",
        "at_version": 7,
    })
    assert r.status_code == 201
    assert r.json()["total_count"] == 0


def test_06_invalid_version_interval_and_unknown_targets(seeded, client):
    latest = client.get(f"{API}/status").json()["current_version"]

    # 版本越界：0（参数校验）与超当前版本（业务校验）都明确 422，不产生半成品
    r = _create(client, at_version=0)
    assert r.status_code == 422
    for bad_v in (latest + 1, 99999):
        r = _create(client, at_version=bad_v)
        assert r.status_code == 422, (bad_v, r.text)
        assert r.json()["error"]["code"] == "invalid_version"
    # 越界后未留下包：find 只读定位，超范围版本上无包 -> 404
    assert client.get(f"{API}/audit/packages/find",
                      params=CRITERIA | {"at_version": latest + 1}).status_code == 404

    # 无效时间区间：起点 >= 终点（含相等）-> 422
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "DA",
        "time_from": "2026-02-01T00:00:00Z",
        "time_to": "2026-02-01T00:00:00Z",
        "at_version": 7,
    })
    assert r.status_code == 422
    # 缺时区 -> 422
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "DA",
        "time_from": "2026-01-01 00:00:00",
        "time_to": "2026-02-01T00:00:00Z",
        "at_version": 7,
    })
    assert r.status_code == 422
    # page_size 越界
    r = _create(client, at_version=7, page_size=201)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_page_size"

    # 未知包 / 未知段 -> 404
    r = client.get(f"{API}/audit/packages/ap-deadbeef")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_package"
    pkg = _create(client, at_version=7, page_size=2).json()
    r = client.get(f"{API}/audit/packages/{pkg['package_id']}/segments/99")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_segment"

    # find 不存在 -> 404
    r = client.get(f"{API}/audit/packages/find", params=CRITERIA | {"at_version": 1})
    assert r.status_code == 404
    # find 无效区间 -> 422（不隐式生成）
    r = client.get(f"{API}/audit/packages/find", params={
        "device_no": "DA",
        "time_from": "2026-02-01T00:00:00Z",
        "time_to": "2026-02-01T00:00:00Z",
        "at_version": 7,
    })
    assert r.status_code == 422


def test_07_timezone_aware_half_open_inputs(seeded, client):
    # +08:00 边界等价 UTC：2026-01-01T08:00:00+08:00 == 2026-01-01T00:00:00Z；
    # 2026-03-01T08:00:00+08:00 == 2026-03-01T00:00:00Z
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "DA",
        "time_from": "2026-01-01T08:00:00+08:00",
        "time_to": "2026-03-01T08:00:00+08:00",
        "at_version": 7,
    })
    assert r.status_code == 201, r.text
    tz_pkg = r.json()
    base = _create(client, at_version=7, page_size=2).json()
    # 时区写法不同但条件等价 -> 同一个包，同一摘要
    assert tz_pkg["package_id"] == base["package_id"]
    assert tz_pkg["criteria"]["time_from"] == "2026-01-01T00:00:00.000000Z"
    assert tz_pkg["criteria"]["time_to"] == "2026-03-01T00:00:00.000000Z"

    # 左闭：起点恰为 A2 时刻时 A2 入选；右开：右界前一刻可入选、右界时刻不入选
    r = client.post(f"{API}/audit/packages", json={
        "device_no": "DA",
        "time_from": "2026-01-15T00:00:00Z",
        "time_to": "2026-02-20T08:30:00Z",
        "at_version": 7,
    })
    assert r.status_code == 201
    seg = client.get(
        f"{API}/audit/packages/{r.json()['package_id']}/segments/0"
    ).json()
    assert [e["reading_no"] for e in seg["entries"]] == ["A2", "A3"]


def test_08_segment_reassembly_completeness(seeded, client):
    # 小页大小 1：逐条分段，拼接全部段 == 全量，顺序稳定无重无漏。
    # 用 v6 视角（首次以该条件+版本建包），避免复用已固定的 v7 分页
    pkg = _create(client, at_version=6, page_size=1).json()
    assert pkg["total_count"] == 4 and pkg["segment_count"] == 4
    seen, offsets = [], []
    for i in range(4):
        seg = client.get(
            f"{API}/audit/packages/{pkg['package_id']}/segments/{i}"
        ).json()
        assert seg["count"] == 1
        assert seg["offset"] == i
        offsets.append(seg["offset"])
        seen.extend(e["reading_no"] for e in seg["entries"])
    assert offsets == list(range(4))
    assert seen == ["A1", "A2", "A3", "A4"]


def test_09_concurrent_commit_during_generation_is_excluded(seeded, client):
    """生成窗口内并发发生的读数提交必须被写锁阻塞，且不混入已冻结的包。

    直接在 service 层用两个真实连接打 SQLite 文件锁：建包事务内、提交前打开
    栅栏让另一线程先尝试 BEGIN IMMEDIATE（必须等待），事务提交后对方才完成。
    """
    import threading

    from app import audit

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    errors: list = []

    # 全新条件+版本，避免唯一约束命中复用路径；区间 [2026-02-01, 2026-03-01)
    # 在 v4 时只有 A3（A9 尚未提交）
    create_data = {
        "device_no": "DA",
        "time_from": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "time_to": datetime(2026, 3, 1, tzinfo=timezone.utc),
        "at_version": 4,
        "page_size": 50,
    }
    original = audit._build_and_store
    pkg_holder: dict = {}

    def patched(conn, *args, **kwargs):
        started.set()
        # 等另一线程已经开始抢锁（BEGIN IMMEDIATE 会阻塞在建包写锁上）
        release.wait(timeout=5)
        return original(conn, *args, **kwargs)

    def concurrent_submit():
        try:
            started.wait(timeout=5)
            # 此时建包事务持有 RESERVED/EXCLUSIVE 写锁
            from app import service
            service.submit_readings({
                "request_id": "a-req-concurrent",
                "readings": [{
                    "reading_no": "A-CONC", "device_no": "DA",
                    "measured_at": datetime(2026, 2, 10, tzinfo=timezone.utc),
                    "value": 15.0,
                }],
            })
            done.set()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def concurrent_create():
        try:
            pkg_holder["pkg"] = audit.create_audit_package(dict(create_data))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    audit._build_and_store = patched
    creator = threading.Thread(target=concurrent_create)
    submitter = threading.Thread(target=concurrent_submit)
    creator.start()
    try:
        # 建包线程进入事务、在落快照前停下
        assert started.wait(timeout=5)
        # 再启动提交线程，使其阻塞在写锁上
        submitter.start()
        time.sleep(0.3)
        assert not done.is_set(), "concurrent write must block while package txn is open"
        release.set()
        creator.join(timeout=5)
        submitter.join(timeout=5)
    finally:
        audit._build_and_store = original
    assert not errors, errors
    assert done.is_set()
    pkg = pkg_holder["pkg"]

    # 包内没有并发期间提交的读数（它的版本晚于 at_version=7）
    seg = client.get(f"{API}/audit/packages/{pkg['package_id']}/segments/0")
    if seg.status_code == 200:
        assert all(e["reading_no"] != "A-CONC" for e in seg.json()["entries"])
    else:
        # 该区间内本就无 v7 前提交读数 -> 空包
        assert seg.status_code == 404 and pkg["total_count"] == 0

    # 并发提交确实在包生成之后完成，读数存在且版本更新（晚于固定视角 v4）
    r = client.get(f"{API}/readings/A-CONC")
    assert r.status_code == 200
    assert r.json()["submitted_at_version"] > 4
    # 同条件+版本再取包：摘要不变（并发提交没有改变冻结结果）
    again = client.get(f"{API}/audit/packages/{pkg['package_id']}").json()
    assert again["package_digest"] == pkg["package_digest"]
