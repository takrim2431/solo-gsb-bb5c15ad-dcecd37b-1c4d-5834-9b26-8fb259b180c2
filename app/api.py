"""HTTP API 路由。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from fastapi import status as http_status

from . import audit, service
from .database import get_conn
from .errors import ApiError
from .models import (
    AuditPackageRequest,
    CertCorrectRequest,
    CertIssueRequest,
    CertRevokeRequest,
    ReadingSubmitRequest,
    UtcDatetime,
    to_utc_string,
)

router = APIRouter(prefix="/api/v1", tags=["traceability"])


@router.get("/health")
def health() -> dict:
    with get_conn() as conn:
        return {"status": "ok", "version": service.current_version(conn)}


@router.get("/status")
def status() -> dict:
    """服务总览：当前单调版本与结论枚举。"""
    with get_conn() as conn:
        return {
            "current_version": service.current_version(conn),
            "conclusion_statuses": list(service.CONCLUSIONS),
        }


# ---------------- 证书：签发 / 纠正 / 撤销

@router.post("/certificates")
def issue_certificate(req: CertIssueRequest) -> dict:
    return service.issue_certificate(req.model_dump())


@router.post("/certificates/{certificate_no}/correct")
def correct_certificate(certificate_no: str, req: CertCorrectRequest) -> dict:
    data = req.model_dump()
    if data["certificate_no"] != certificate_no:
        raise ApiError(422, "path_body_mismatch",
                       "certificate_no in path and body must match")
    return service.correct_certificate(data)


@router.post("/certificates/{certificate_no}/revoke")
def revoke_certificate(certificate_no: str, req: CertRevokeRequest) -> dict:
    data = req.model_dump()
    if data["certificate_no"] != certificate_no:
        raise ApiError(422, "path_body_mismatch",
                       "certificate_no in path and body must match")
    return service.revoke_certificate(data)


# ---------------- 证书查询

@router.get("/certificates")
def list_certificates(
    at_version: Optional[int] = Query(default=None, ge=1),
    device_no: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_certificates(conn, at_version, device_no, limit, offset)


@router.get("/certificates/{certificate_no}")
def get_certificate(
    certificate_no: str,
    at_version: Optional[int] = Query(default=None, ge=1),
) -> dict:
    with get_conn() as conn:
        out = service.get_certificate(conn, certificate_no, at_version)
        if out is None:
            raise ApiError(404, "unknown_certificate",
                           f"certificate {certificate_no!r} not found")
        return out


@router.get("/certificates/{certificate_no}/history")
def certificate_history(certificate_no: str) -> dict:
    with get_conn() as conn:
        return service.certificate_history(conn, certificate_no)


# ---------------- 读数：提交与查询

@router.post("/readings")
def submit_readings(req: ReadingSubmitRequest) -> dict:
    return service.submit_readings(req.model_dump())


@router.get("/readings")
def list_readings(
    at_version: Optional[int] = Query(default=None, ge=1),
    device_no: Optional[str] = Query(default=None, max_length=128),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_readings(conn, at_version, device_no, status_filter, limit, offset)


@router.get("/readings/{reading_no}")
def get_reading(
    reading_no: str,
    at_version: Optional[int] = Query(default=None, ge=1),
) -> dict:
    with get_conn() as conn:
        out = service.get_reading(conn, reading_no, at_version)
        if out is None:
            raise ApiError(404, "unknown_reading", f"reading {reading_no!r} not found")
        return out


# ---------------- 版本与请求追溯

@router.get("/versions")
def list_versions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_versions(conn, limit, offset)


@router.get("/requests/{request_id}")
def get_request(request_id: str) -> dict:
    with get_conn() as conn:
        out = service.get_request(conn, request_id)
        if out is None:
            raise ApiError(404, "unknown_request",
                           f"request_id {request_id!r} not found")
        return out


# ---------------- 审计取证追溯包

@router.post("/audit/packages", status_code=http_status.HTTP_201_CREATED)
def create_audit_package(req: AuditPackageRequest) -> dict:
    """按 设备编号 + 半开测量时间区间 + 已提交版本 生成（或幂等复用）追溯包。"""
    return audit.create_audit_package(req.model_dump())


@router.get("/audit/packages/find")
def find_audit_package(
    device_no: str = Query(min_length=1, max_length=128),
    time_from: UtcDatetime = Query(),
    time_to: UtcDatetime = Query(),
    at_version: int = Query(ge=1),
) -> dict:
    """按筛选条件+版本定位既有追溯包；不存在返回 404，不会隐式生成。"""
    if time_from >= time_to:
        raise ApiError(
            422, "invalid_time_interval",
            "time_from must be earlier than time_to (half-open interval [time_from, time_to))",
        )
    out = audit.find_audit_package(
        device_no, to_utc_string(time_from), to_utc_string(time_to), at_version
    )
    if out is None:
        raise ApiError(404, "unknown_package", "no audit package for the given criteria")
    out["reused"] = True
    return out


@router.get("/audit/packages/{package_id}")
def get_audit_package(package_id: str) -> dict:
    """追溯包清单：筛选条件、版本、总条数、分段表与可复算完整性摘要。"""
    with get_conn() as conn:
        out = audit.get_audit_package(conn, package_id)
        if out is None:
            raise ApiError(404, "unknown_package",
                           f"audit package {package_id!r} not found")
        return out


@router.get("/audit/packages/{package_id}/segments/{segment_index}")
def get_audit_segment(package_id: str, segment_index: int) -> dict:
    """分段获取冻结条目；段号从 0 起，拼接全部段即可无遗漏、无重复地复原整包。"""
    with get_conn() as conn:
        out = audit.get_audit_segment(conn, package_id, segment_index)
        if out is None:
            raise ApiError(404, "unknown_package",
                           f"audit package {package_id!r} not found")
        return out
