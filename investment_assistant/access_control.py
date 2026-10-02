"""R6 单机团队身份边界：最小真实鉴权、资源归属绑定与认证/授权分层。

边界说明（不夸大）
------------------
* 这是**单服务、本地演示级真实鉴权**：token → actor/tenant/roles 映射由本地环境变量
  ``IA_AUTH_TOKENS`` 提供（JSON），服务端常量时间比较；不写明文 token 到日志或磁盘。
  它不等于企业 OIDC/SSO 或生产密钥管理。
* 无配置、配置损坏、无效 token 一律 fail-closed 拒绝；不为旧接口保留无认证旁路。
* ``requested_by``、ticker、report_id、前端 session 都不是身份或授权。
* 老报告没有可信归属时隔离为 ``migration_pending``，绝不默认公有。
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any, Callable

ROLE_ANALYST = "analyst"
ROLE_REVIEWER = "reviewer"
ROLE_PUBLISHER = "publisher"
ROLE_ADMIN = "admin"
KNOWN_ROLES = frozenset({ROLE_ANALYST, ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_ADMIN})

AUTH_TOKEN_ENV = "IA_AUTH_TOKENS"


class AccessDeniedError(Exception):
    """鉴权/授权失败基类；``error_code`` 稳定可序列化，不泄露资源存在性。"""

    error_code = "ACCESS_DENIED"

    def __init__(self, message: str = "访问被拒绝。", error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class Unauthorized(AccessDeniedError):
    error_code = "AUTH_REQUIRED"


class Forbidden(AccessDeniedError):
    error_code = "FORBIDDEN"


@dataclass(frozen=True)
class Principal:
    """一次已验证的请求身份；字段只能由服务端从 token 配置推导，不接受请求参数。"""

    actor_id: str
    tenant_id: str
    roles: frozenset[str]

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def _parse_token_config(raw: str | None) -> dict[str, dict[str, Any]] | None:
    """解析 token 配置；无配置或配置损坏时返回 None（fail-closed）。"""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    config: dict[str, dict[str, Any]] = {}
    for token, entry in parsed.items():
        if not isinstance(token, str) or not token.strip() or not isinstance(entry, dict):
            return None
        actor = str(entry.get("actor") or "").strip()
        tenant = str(entry.get("tenant") or "").strip()
        roles = entry.get("roles")
        if not actor or not tenant or not isinstance(roles, list):
            return None
        normalized_roles = {str(role).strip() for role in roles if str(role).strip()}
        if not normalized_roles or not normalized_roles.issubset(KNOWN_ROLES):
            return None
        expiry = entry.get("expires_at")
        if expiry is not None:
            try:
                parsed_expiry = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if parsed_expiry.tzinfo is None:
                    return None
            except ValueError:
                return None
        config[token] = {"actor": actor, "tenant": tenant, "roles": normalized_roles, "expires_at": expiry}
    return config


class TokenAuthService:
    """本地 token → 身份映射；常量时间比较，不落盘、不打明文 token。"""

    def __init__(self, provider: Callable[[], str | None]) -> None:
        self._provider = provider

    def _config(self) -> dict[str, dict[str, Any]] | None:
        return _parse_token_config(self._provider())

    def authenticate(self, token: str | None) -> Principal:
        """验证 token 并返回 Principal；任何失败路径都是 fail-closed。"""
        config = self._config()
        if config is None:
            raise Unauthorized("服务未配置身份认证，已拒绝访问。", "AUTH_NOT_CONFIGURED")
        if not token or not str(token).strip():
            raise Unauthorized("缺少身份凭据。", "AUTH_MISSING")
        candidate = str(token)
        matched: dict[str, Any] | None = None
        # 遍历全部候选做常量时间比较，避免按候选顺序/长度泄露信息。
        for configured_token, entry in config.items():
            if hmac.compare_digest(configured_token.encode("utf-8"), candidate.encode("utf-8")):
                matched = entry
        if matched is None:
            raise Unauthorized("身份凭据无效。", "AUTH_INVALID")
        if matched.get("expires_at") and datetime.fromisoformat(str(matched["expires_at"]).replace("Z", "+00:00")) <= datetime.now(UTC):
            raise Unauthorized("身份凭据已过期。", "AUTH_EXPIRED")
        return Principal(actor_id=str(matched["actor"]), tenant_id=str(matched["tenant"]), roles=frozenset(matched["roles"]))

    def require_roles(self, principal: Principal, *roles: str) -> None:
        if not principal.has_role(*roles):
            raise Forbidden("当前身份没有执行该操作的角色。", "ROLE_REQUIRED")


def env_token_provider() -> str | None:
    return os.getenv(AUTH_TOKEN_ENV)


default_auth_service = TokenAuthService(env_token_provider)


# --- 报告/任务归属 ---------------------------------------------------------------


def bind_report_ownership(result: dict[str, Any], principal: Principal) -> None:
    """把服务端推导的归属写入工作流结果 dict（persist_report 会随审计 JSON 落盘）。

    请求参数（requested_by 等）不能覆盖归属；这是服务端绑定，不可由请求者自行修改。
    """
    result["tenant_id"] = principal.tenant_id
    result["actor_id"] = principal.actor_id


def report_ownership_from_audit(audit: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """读取报告归属。返回 ``(tenant_id, actor_id, migration_pending)``。

    审计 JSON 缺少归属字段的老报告视为 ``migration_pending``，绝不默认公有。
    """
    tenant = str(audit.get("tenant_id") or "").strip() or None
    actor = str(audit.get("actor_id") or "").strip() or None
    return tenant, actor, tenant is None


def check_report_tenant_access(audit: dict[str, Any], principal: Principal) -> bool:
    """返回当前 principal 是否可访问该报告；migration_pending 仅 admin 可见。"""
    tenant, _actor, migration_pending = report_ownership_from_audit(audit)
    if migration_pending:
        return principal.has_role(ROLE_ADMIN)
    return tenant == principal.tenant_id
