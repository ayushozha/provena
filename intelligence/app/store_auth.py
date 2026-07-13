"""Authenticate intelligence ingress and forward only store credentials."""

from hmac import compare_digest

from fastapi import HTTPException
from fastapi import Request

from app.config import IntelligenceSettings

STORE_AUTH_HEADER_ALLOWLIST = (
    "Authorization",
    "X-Provena-Tenant-Id",
    "X-Provena-Role",
    "X-Provena-Key-Id",
    "X-Provena-Principal-Id",
    "X-Provena-Groups",
)

ALLOWED_ROLES = {"viewer", "editor", "admin", "superadmin"}
WRITE_ROLES = {"editor", "admin", "superadmin"}


def _secret(settings: IntelligenceSettings, name: str) -> str:
    value = getattr(settings, name)
    return value.get_secret_value().strip() if value else ""


def _bearer(request: Request) -> str:
    scheme, separator, credential = request.headers.get("Authorization", "").partition(" ")
    if separator and scheme.strip().lower() == "bearer":
        return credential.strip()
    return ""


def authenticate_intelligence_request(
    request: Request,
    settings: IntelligenceSettings,
) -> str:
    """Return the authoritative role or reject before any pipeline work begins."""
    environment = settings.environment.strip().lower()
    local_bypass = (
        environment in {"development", "local", "test"}
        and settings.allow_unauthenticated_local
    )
    role = request.headers.get("X-Provena-Role", "").strip().lower()
    if local_bypass:
        if role and role not in ALLOWED_ROLES:
            raise HTTPException(status_code=403, detail="invalid Provena role")
        return role or "superadmin"

    credential = _bearer(request)
    gateway_token = _secret(settings, "gateway_service_token")
    service_token = _secret(settings, "service_token")
    gateway_match = bool(credential and gateway_token) and compare_digest(
        credential, gateway_token
    )
    service_match = bool(credential and service_token) and compare_digest(
        credential, service_token
    )
    if not gateway_match and not service_match:
        raise HTTPException(status_code=401, detail="valid intelligence bearer required")

    tenant_id = request.headers.get("X-Provena-Tenant-Id", "").strip()
    principal_id = request.headers.get("X-Provena-Principal-Id", "").strip()
    key_id = request.headers.get("X-Provena-Key-Id", "").strip()
    if role not in ALLOWED_ROLES or not principal_id or not key_id:
        raise HTTPException(status_code=401, detail="valid intelligence identity headers required")
    if role != "superadmin" and not tenant_id:
        raise HTTPException(status_code=401, detail="tenant-bound intelligence identity required")

    if service_match and (
        tenant_id != settings.service_tenant_id.strip()
        or principal_id != settings.service_principal_id.strip()
        or role != settings.service_role.strip().lower()
    ):
        raise HTTPException(status_code=403, detail="intelligence service identity mismatch")
    return role


def require_intelligence_write(request: Request) -> None:
    if getattr(request.state, "provena_role", "") not in WRITE_ROLES:
        raise HTTPException(status_code=403, detail="write access required")


def store_request_headers(request: Request) -> dict[str, str]:
    """Return only the request credentials the authenticated store may evaluate."""
    return {
        name: value
        for name in STORE_AUTH_HEADER_ALLOWLIST
        if (value := request.headers.get(name))
    }
