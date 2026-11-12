import logging
import uuid

from odoo.http import Controller, request
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class BrewApiController(Controller):
    def _request_id(self):
        incoming = request.httprequest.headers.get("X-Request-ID", "").strip()
        return incoming[:100] if incoming else f"req_{uuid.uuid4().hex}"

    def _json_body(self):
        if not request.httprequest.data:
            return {}
        try:
            payload = request.get_json_data()
        except (TypeError, ValueError) as exc:
            raise ApiError("VALIDATION_FORMAT", "请求体必须是有效的 JSON。", 400) from exc
        if not isinstance(payload, dict):
            raise ApiError("VALIDATION_FORMAT", "请求体必须是 JSON 对象。", 400)
        return payload

    def _response(self, data=None, status=200, meta=None, error=None, request_id=None):
        request_id = request_id or self._request_id()
        body = {
            "data": data,
            "meta": meta or {},
            "error": error,
            "request_id": request_id,
        }
        return request.make_json_response(
            body,
            status=status,
            headers=[("X-Request-ID", request_id), ("X-API-Version", "1")],
        )

    def _error_response(self, error, request_id=None):
        # Returning a JSON error bypasses Odoo's exception rollback, so roll
        # back explicitly to prevent partially committed command handlers.
        request.env.cr.rollback()
        if isinstance(error, ApiError):
            return self._response(
                status=error.status,
                error={"code": error.code, "message": error.message, "details": error.details},
                request_id=request_id,
            )
        if isinstance(error, ValidationError):
            return self._response(status=422, error={"code": "VALIDATION_ERROR", "message": str(error), "details": {}}, request_id=request_id)
        request_id = request_id or self._request_id()
        _logger.exception("Unhandled API error request_id=%s", request_id)
        return self._response(
            status=500,
            error={"code": "INTERNAL_ERROR", "message": "服务暂时不可用。", "details": {}},
            request_id=request_id,
        )

    def _bearer_token(self):
        authorization = request.httprequest.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    def _authenticate(self):
        token = self._bearer_token()
        api_session = request.env["brew.api.session"].sudo().authenticate_access_token(token) if token else None
        if api_session:
            user = api_session.user_id
            company = api_session.company_id
            context = {**request.env.context, "allowed_company_ids": [company.id]}
            if api_session.factory_id:
                context["brew_factory_id"] = api_session.factory_id.id
            request.update_env(user=user.id, context=context)
            return user, company, api_session, "bearer"

        if request.session.uid:
            request.update_env(user=request.session.uid)
            user = request.env.user
            if not user.active:
                raise ApiError("AUTH_SESSION_REVOKED", "登录会话已失效。", 401)
            company = request.env.company
            if company not in user.company_ids:
                company = user.company_id
            context = {**request.env.context, "allowed_company_ids": [company.id]}
            session_context = getattr(request.session, "context", {}) or {}
            if session_context.get("brew_factory_id"):
                context["brew_factory_id"] = session_context["brew_factory_id"]
            request.update_env(user=user.id, context=context)
            return user, company, None, "cookie"
        raise ApiError("AUTH_REQUIRED", "请先登录。", 401)

    def _set_cookie_context(self, company, factory=None):
        session_context = dict(getattr(request.session, "context", {}) or {})
        session_context["allowed_company_ids"] = [company.id]
        if factory:
            session_context["brew_factory_id"] = factory.id
        else:
            session_context.pop("brew_factory_id", None)
        request.session.context = session_context
        request.update_env(user=request.env.user.id, context={**request.env.context, **session_context})

    def _validate_cookie_csrf(self, auth_mode):
        if auth_mode != "cookie":
            return
        csrf_token = request.httprequest.headers.get("X-CSRF-Token", "")
        if not request.validate_csrf(csrf_token):
            raise ApiError("AUTH_CSRF_INVALID", "CSRF 校验失败。", 403)

    def _audit(self, event_type, action, request_id, user=None, company=None, summary=None):
        request.env["brew.audit.log"].sudo().log_event(
            event_type,
            action,
            user=user or request.env.user,
            company=company or request.env.company,
            request_id=request_id,
            ip_address=request.httprequest.remote_addr,
            user_agent=request.httprequest.headers.get("User-Agent", "")[:500],
            summary=summary or {},
        )

    def _require_group(self, user, xml_id):
        if not user.has_group(xml_id):
            raise ApiError("PERMISSION_DENIED", "当前账号无权执行该操作。", 403)

    def _start_idempotency(self, user, company, payload):
        key = request.httprequest.headers.get("Idempotency-Key", "").strip()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "该操作必须提供幂等键。", 400)
        if len(key) > 200:
            raise ApiError("VALIDATION_FORMAT", "幂等键长度不能超过 200。", 422)
        try:
            record, replay = request.env["brew.idempotency.record"].sudo().begin(
                company,
                user,
                request.httprequest.method,
                request.httprequest.path,
                key,
                payload,
            )
        except Exception as exc:
            message = str(exc)
            if "IDEMPOTENCY_KEY_REUSED" in message:
                raise ApiError("IDEMPOTENCY_KEY_REUSED", "该幂等键已用于不同请求。", 409) from exc
            if "IDEMPOTENCY_IN_PROGRESS" in message:
                raise ApiError("IDEMPOTENCY_IN_PROGRESS", "相同操作正在处理中。", 409) from exc
            raise
        if replay:
            status, data = record.stored_response()
            return record, self._response(data, status=status)
        return record, None

    def _complete_idempotency(self, record, data, status=200):
        record.complete(status, data)
        return self._response(data, status=status)
