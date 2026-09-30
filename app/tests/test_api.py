"""端到端测试：通过 TestClient 覆盖全部业务规则。"""
from __future__ import annotations

import os
import pathlib

import pytest

# 必须在导入应用代码前设置数据库路径
DB_PATH = "/tmp/trace_test_%d.db" % os.getpid()
for suffix in ("", "-wal", "-shm"):
    p = pathlib.Path(DB_PATH + suffix)
    if p.exists():
        p.unlink()
os.environ["TRACE_DB_PATH"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reset_settings_cache  # noqa: E402
from app.main import app  # noqa: E402


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


API = "/api/v1"


def test_01_issue_and_conforming_reading(client):
    # 签发证书：设备 D1，有效期 [2026-01-01, 2026-02-01)，允许值 [10.0, 20.0]
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-1",
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 10.0,
        "max_value": 20.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["event_type"] == "cert_issue"

    # 提交合格读数
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-1",
        "readings": [{
            "reading_no": "R1", "device_no": "D1",
            "measured_at": "2026-01-15T08:00:00Z", "value": 15.5,
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["results"][0]["status"] == "conforming"
    assert body["results"][0]["reliance"]["certificate_no"] == "CERT-1"

    # 超限读数
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-2",
        "readings": [{
            "reading_no": "R2", "device_no": "D1",
            "measured_at": "2026-01-16T08:00:00Z", "value": 21.0,
        }],
    })
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "out_of_range"

    # 未验证读数：时间在左闭右开区间外（右边界）
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-3",
        "readings": [{
            "reading_no": "R3", "device_no": "D1",
            "measured_at": "2026-02-01T00:00:00Z", "value": 15.0,
        }, {
            "reading_no": "R4", "device_no": "D2",
            "measured_at": "2026-01-10T00:00:00Z", "value": 1.0,
        }],
    })
    assert r.status_code == 200, r.text
    statuses = {x["reading_no"]: x["status"] for x in r.json()["results"]}
    assert statuses["R3"] == "unverified"
    assert statuses["R4"] == "unverified"


def test_02_half_open_boundary(client):
    # 左边界包含
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-boundary",
        "readings": [{
            "reading_no": "R-EDGE", "device_no": "D1",
            "measured_at": "2026-01-01T00:00:00Z", "value": 10.0,
        }],
    })
    assert r.json()["results"][0]["status"] == "conforming"
    # 闭区间边界值
    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "conforming"


def test_03_multiple_matches_review(client):
    # 为 D1 再签一份覆盖同一测量时刻的证书 -> 多份匹配待复核
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-2",
        "certificate_no": "CERT-2",
        "device_no": "D1",
        "valid_from": "2026-01-10T00:00:00Z",
        "valid_to": "2026-01-20T00:00:00Z",
        "min_value": 0.0,
        "max_value": 100.0,
    })
    assert r.status_code == 200
    # 事件序：CERT-1=v1, 三批读数=v2/v3/v4, 边界读数=v5, CERT-2=v6
    assert r.json()["version"] == 6

    r = client.get(f"{API}/readings/R1")
    body = r.json()
    assert body["status"] == "review"
    assert {c["certificate_no"] for c in body["candidates"]} == {"CERT-1", "CERT-2"}


def test_04_correct_certificate_changes_conclusions(client):
    # 纠正 CERT-2：把范围收紧，使 R1 唯一匹配时（后续撤销 CERT-1 前先纠正）
    # 这里直接纠正 CERT-1 的允许值范围，15.5 变为超限
    r = client.post(f"{API}/certificates/CERT-1/correct", json={
        "request_id": "req-cert-1-fix",
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 16.0,
        "max_value": 20.0,
        "reason": "range corrected",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["superseded_version_seq"] == 1
    assert body["certificate"]["version_seq"] == 2
    fix_version = body["version"]

    # 受影响读数中应能看到 R1/R2 的变化记录（此刻多匹配仍可能为 review，校验结构存在）
    affected = {a["reading_no"]: a for a in body["affected_readings"]}
    assert "R1" in affected and "R2" in affected
    assert set(affected["R1"]["before"]) >= {"at_version", "status"}

    # 当前视角：R1 同时匹配两份仍为 review；撤销 CERT-2 后唯一匹配 CERT-1 v2 -> 超限
    r = client.post(f"{API}/certificates/CERT-2/revoke", json={
        "request_id": "req-cert-2-revoke",
        "certificate_no": "CERT-2",
        "reason": "duplicate calibration",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    revoke_version = body["version"]
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # R1: review -> out_of_range（15.5 不在 [16,20]）
    assert by_no["R1"]["before"]["status"] == "review"
    assert by_no["R1"]["after"]["status"] == "out_of_range"
    assert by_no["R1"]["conclusion_changed"] is True
    assert by_no["R1"]["after"]["reliance"]["version_seq"] == 2

    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "out_of_range"

    # 旧版本结论仍可查询：在纠正前的版本视角，R1 对 CERT-1 v1 合格（但彼时 CERT-2 未撤销仍 review）
    r = client.get(f"{API}/readings/R1", params={"at_version": fix_version - 1})
    old = r.json()
    assert old["at_version"] == fix_version - 1
    assert old["status"] == "review"  # CERT-1 与 CERT-2 均有效

    # 在读数提交当时（v2，CERT-2 尚未签发），R1 唯一匹配 CERT-1 v1，合格
    r = client.get(f"{API}/readings/R1", params={"at_version": 2})
    assert r.json()["status"] == "conforming"
    assert r.json()["reliance"]["version_seq"] == 1

    # 撤销后版本：明确超限
    r = client.get(f"{API}/readings/R1", params={"at_version": revoke_version})
    assert r.json()["status"] == "out_of_range"


def test_05_certificate_history_immutable(client):
    r = client.get(f"{API}/certificates/CERT-1/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_version_seq"] == 2
    assert len(body["versions"]) == 2
    v1, v2 = body["versions"]
    assert v1["version_seq"] == 1 and v1["superseded_at_version"] is not None
    assert v2["version_seq"] == 2 and v2["superseded_at_version"] is None
    # 旧版本内容没有被覆盖
    assert v1["min_value"] == 10.0 and v2["min_value"] == 16.0

    # at_version 视角能取到当时的证书内容
    r = client.get(f"{API}/certificates/CERT-1", params={"at_version": 1})
    one = r.json()
    assert one["version_seq"] == 1 and one["status"] == "active"
    r = client.get(f"{API}/certificates/CERT-1")
    cur = r.json()
    assert cur["version_seq"] == 2 and cur["status"] == "active"
    r = client.get(f"{API}/certificates/CERT-2")
    assert r.json()["status"] == "revoked"


def test_06_idempotency_same_request(client):
    payload = {
        "request_id": "req-cert-1",  # 与签发 CERT-1 完全相同
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 10.0,
        "max_value": 20.0,
    }
    r = client.post(f"{API}/certificates", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent_replay"] is True
    assert body["version"] == 1

    before = client.get(f"{API}/status").json()["current_version"]
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-1",
        "readings": [{
            "reading_no": "R1", "device_no": "D1",
            "measured_at": "2026-01-15T08:00:00Z", "value": 15.5,
        }],
    })
    assert r.json()["idempotent_replay"] is True
    after = client.get(f"{API}/status").json()["current_version"]
    assert before == after


def test_07_request_id_reuse_conflict_and_invalid_inputs(client):
    # 同一 request_id 不同内容 -> 409
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-1",
        "certificate_no": "CERT-X",
        "device_no": "D9",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_id_conflict"
    # 冲突后未产生新版本
    assert client.get(f"{API}/certificates/CERT-X").status_code == 404

    # 无效区间：valid_from >= valid_to
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-1",
        "certificate_no": "CERT-BAD",
        "device_no": "D1",
        "valid_from": "2026-02-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422

    # min > max
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-2",
        "certificate_no": "CERT-BAD",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 9.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422
    # 失败不留痕
    assert client.get(f"{API}/certificates/CERT-BAD").status_code == 404

    # 无时区时间 -> 422
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-3",
        "certificate_no": "CERT-NAIVE",
        "device_no": "D1",
        "valid_from": "2026-01-01 00:00:00",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422

    # 未知目标：纠正/撤销不存在的证书
    assert client.post(f"{API}/certificates/NOPE/correct", json={
        "request_id": "req-bad-4", "certificate_no": "NOPE", "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0, "max_value": 1.0,
    }).status_code == 404
    assert client.post(f"{API}/certificates/NOPE/revoke", json={
        "request_id": "req-bad-5", "certificate_no": "NOPE",
    }).status_code == 404

    # 撤销已撤销证书 -> 409；纠正已撤销证书 -> 409
    assert client.post(f"{API}/certificates/CERT-2/revoke", json={
        "request_id": "req-double-revoke", "certificate_no": "CERT-2",
    }).status_code == 409
    assert client.post(f"{API}/certificates/CERT-2/correct", json={
        "request_id": "req-fix-revoked", "certificate_no": "CERT-2", "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0, "max_value": 1.0,
    }).status_code == 409

    # 读数不可变：重复读数编号整批失败，不留部分变更
    r = client.post(f"{API}/readings", json={
        "request_id": "req-bad-read",
        "readings": [
            {"reading_no": "R1", "device_no": "D1",
             "measured_at": "2026-01-15T08:00:00Z", "value": 1.0},
            {"reading_no": "R-NEW", "device_no": "D1",
             "measured_at": "2026-01-15T09:00:00Z", "value": 1.0},
        ],
    })
    assert r.status_code == 409
    assert client.get(f"{API}/readings/R-NEW").status_code == 404

    # 批次内 reading_no 重复 -> 422
    r = client.post(f"{API}/readings", json={
        "request_id": "req-bad-read2",
        "readings": [
            {"reading_no": "X", "device_no": "D1",
             "measured_at": "2026-01-15T08:00:00Z", "value": 1.0},
            {"reading_no": "X", "device_no": "D1",
             "measured_at": "2026-01-15T09:00:00Z", "value": 2.0},
        ],
    })
    assert r.status_code == 422


def test_08_revoke_unverified_and_versions_log(client):
    # 撤销 CERT-1：R2（21.0）等由 out/review 变 unverified
    r = client.post(f"{API}/certificates/CERT-1/revoke", json={
        "request_id": "req-cert-1-revoke",
        "certificate_no": "CERT-1",
        "reason": "end of life",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_type"] == "cert_revoke"
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # R1: out_of_range -> unverified
    assert by_no["R1"]["before"]["status"] == "out_of_range"
    assert by_no["R1"]["after"]["status"] == "unverified"
    assert by_no["R1"]["conclusion_changed"] is True

    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "unverified"
    assert r.json()["reliance"] is None

    # 版本流水完整可查
    r = client.get(f"{API}/versions", params={"limit": 200})
    events = {e["event_type"] for e in r.json()["items"]}
    assert {"cert_issue", "cert_correct", "cert_revoke", "reading_submit"} <= events
    versions = [e["version"] for e in r.json()["items"]]
    assert versions == sorted(versions, reverse=True)

    # 请求追溯
    r = client.get(f"{API}/requests/req-cert-1-fix")
    assert r.status_code == 200
    assert r.json()["event_type"] == "cert_correct"


def test_09_lists_and_filters(client):
    r = client.get(f"{API}/certificates", params={"device_no": "D1"})
    nos = {c["certificate_no"] for c in r.json()["items"]}
    assert {"CERT-1", "CERT-2"} <= nos

    r = client.get(f"{API}/readings", params={"status": "unverified"})
    assert all(i["status"] == "unverified" for i in r.json()["items"])
    assert any(i["reading_no"] == "R1" for i in r.json()["items"])

    # at_version 越界
    assert client.get(f"{API}/readings/R1", params={"at_version": 99999}).status_code == 422


def test_10_timezone_normalization(client):
    # +08:00 时间应等价于对应 UTC：2026-01-15T16:00:00+08:00 == 08:00Z
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-tz",
        "certificate_no": "CERT-TZ",
        "device_no": "DTZ",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2026-02-01T00:00:00+00:00",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 200, r.text
    assert r.json()["certificate"]["valid_from"] == "2026-01-01T00:00:00.000000Z"

    r = client.post(f"{API}/readings", json={
        "request_id": "req-tz-read",
        "readings": [{
            "reading_no": "RTZ", "device_no": "DTZ",
            "measured_at": "2026-01-15T16:00:00+08:00", "value": 0.5,
        }],
    })
    assert r.json()["results"][0]["status"] == "conforming"
