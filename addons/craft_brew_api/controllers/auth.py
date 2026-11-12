from odoo import http
from odoo.exceptions import AccessDenied, AccessError
from odoo.http import request

from .base import ApiError, BrewApiController


class AuthController(BrewApiController):
    @http.route("/api/v1/auth/login", type="http", auth="public", methods=["POST"], csrf=False)
    def login(self):
        request_id = self._request_id()
        authenticated = False
        try:
            payload = self._json_body()
            login = str(payload.get("login") or "").strip()
            password = payload.get("password") or ""
            if not login or not password:
                raise ApiError("VALIDATION_REQUIRED", "账号和密码不能为空。", 422)
            if not request.db:
                raise ApiError("AUTH_DATABASE_UNAVAILABLE", "登录数据库不可用。", 503)
            try:
                uid = request.session.authenticate(request.db, login, password)
            except AccessDenied as exc:
                raise ApiError("AUTH_INVALID_CREDENTIALS", "账号或密码错误。", 401) from exc
            if not uid:
                raise ApiError("AUTH_INVALID_CREDENTIALS", "账号或密码错误。", 401)
            authenticated = True
            request.update_env(user=uid)
            user = request.env.user
            if not user.active:
                raise ApiError("AUTH_SESSION_REVOKED", "账号已停用。", 401)

            company_id = payload.get("company_id")
            company = user.company_id
            if company_id is not None:
                company = user.company_ids.filtered(lambda item: str(item.id) == str(company_id))[:1]
            if company not in user.company_ids:
                raise ApiError("DATA_SCOPE_DENIED", "无权访问所选企业。", 403)

            factory_id = payload.get("factory_id")
            factories = user.brew_allowed_factories(company)
            factory = factories.filtered(lambda item: str(item.id) == str(factory_id))[:1]
            if factory_id is not None and not factory:
                raise ApiError("DATA_SCOPE_DENIED", "无权访问所选厂区。", 403)
            factory = factory or user.brew_default_factory_id
            if factory not in factories:
                factory = factories[:1]

            device_payload = payload.get("device") if isinstance(payload.get("device"), dict) else {}
            device = {
                **device_payload,
                "user_agent": request.httprequest.headers.get("User-Agent", "")[:500],
                "ip_address": request.httprequest.remote_addr,
            }
            session, access_token, refresh_token = request.env["brew.api.session"].sudo().issue(
                user, company, device
            )
            if factory:
                session.write({"factory_id": factory.id})
            self._set_cookie_context(company, factory)
            self._audit(
                "authentication",
                "login",
                request_id,
                user=user,
                company=company,
                summary={"session_id": session.id, "device_name": session.device_name},
            )
            data = {
                **session.token_payload(access_token, refresh_token),
                "context": user.brew_context_payload(),
                "csrf_token": request.csrf_token(),
            }
            return self._response(data, request_id=request_id)
        except Exception as exc:
            if authenticated:
                request.session.logout(keep_db=True)
            return self._error_response(exc, request_id)

    @http.route("/api/v1/auth/refresh", type="http", auth="public", methods=["POST"], csrf=False)
    def refresh(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            refresh_token = payload.get("refresh_token") or ""
            if not refresh_token:
                raise ApiError("VALIDATION_REQUIRED", "刷新凭证不能为空。", 422)
            try:
                session, access_token, new_refresh_token = request.env["brew.api.session"].sudo().rotate(
                    refresh_token
                )
            except AccessError as exc:
                raise ApiError("AUTH_TOKEN_EXPIRED", "刷新凭证无效或已过期。", 401) from exc
            request.update_env(
                user=session.user_id.id,
                context={
                    **request.env.context,
                    "allowed_company_ids": [session.company_id.id],
                    **({"brew_factory_id": session.factory_id.id} if session.factory_id else {}),
                },
            )
            return self._response(
                {
                    **session.token_payload(access_token, new_refresh_token),
                    "context": session.user_id.brew_context_payload(),
                },
                request_id=request_id,
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/auth/me", type="http", auth="public", methods=["GET"], csrf=False)
    def me(self):
        request_id = self._request_id()
        try:
            user, _company, _session, mode = self._authenticate()
            data = user.brew_context_payload()
            if mode == "cookie":
                data["csrf_token"] = request.csrf_token()
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/auth/logout", type="http", auth="public", methods=["POST"], csrf=False)
    def logout(self):
        request_id = self._request_id()
        try:
            user, company, api_session, auth_mode = self._authenticate()
            self._validate_cookie_csrf(auth_mode)
            if api_session:
                api_session.action_revoke("logout")
            self._audit("authentication", "logout", request_id, user=user, company=company)
            request.session.logout(keep_db=True)
            return self._response({"logged_out": True}, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/context/switch-company", type="http", auth="public", methods=["POST"], csrf=False)
    def switch_company(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, _company, api_session, auth_mode = self._authenticate()
            self._validate_cookie_csrf(auth_mode)
            company = user.company_ids.filtered(lambda item: str(item.id) == str(payload.get("company_id")))[:1]
            if not company:
                raise ApiError("DATA_SCOPE_DENIED", "无权访问所选企业。", 403)
            factories = user.brew_allowed_factories(company)
            factory = user.brew_default_factory_id
            if factory not in factories:
                factory = factories[:1]
            if api_session:
                api_session.write(
                    {"company_id": company.id, "factory_id": factory.id if factory else False}
                )
                request.update_env(
                    user=user.id,
                    context={
                        **request.env.context,
                        "allowed_company_ids": [company.id],
                        **({"brew_factory_id": factory.id} if factory else {}),
                    },
                )
            else:
                self._set_cookie_context(company, factory)
            self._audit(
                "context",
                "switch_company",
                request_id,
                user=user,
                company=company,
                summary={"company_id": company.id},
            )
            return self._response(user.brew_context_payload(), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/context/switch-factory", type="http", auth="public", methods=["POST"], csrf=False)
    def switch_factory(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, api_session, auth_mode = self._authenticate()
            self._validate_cookie_csrf(auth_mode)
            factory = user.brew_allowed_factories(company).filtered(
                lambda item: item.active
                and item.company_id == company
                and str(item.id) == str(payload.get("factory_id"))
            )[:1]
            if not factory:
                raise ApiError("DATA_SCOPE_DENIED", "无权访问所选厂区。", 403)
            if api_session:
                api_session.write({"factory_id": factory.id})
                request.update_env(
                    user=user.id,
                    context={**request.env.context, "brew_factory_id": factory.id},
                )
            else:
                self._set_cookie_context(company, factory)
            self._audit(
                "context",
                "switch_factory",
                request_id,
                user=user,
                company=company,
                summary={"factory_id": factory.id},
            )
            return self._response(user.brew_context_payload(), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/auth/sessions", type="http", auth="public", methods=["GET"], csrf=False)
    def sessions(self):
        request_id = self._request_id()
        try:
            user, _company, current_session, _mode = self._authenticate()
            sessions = request.env["brew.api.session"].sudo().search(
                [("user_id", "=", user.id), ("revoked_at", "=", False)],
                order="last_seen_at desc, id desc",
            )
            data = [
                {
                    "id": str(item.id),
                    "device_name": item.device_name,
                    "company_id": str(item.company_id.id),
                    "factory_id": str(item.factory_id.id) if item.factory_id else None,
                    "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
                    "created_at": item.create_date.isoformat() if item.create_date else None,
                    "is_current": bool(current_session and current_session.id == item.id),
                }
                for item in sessions
            ]
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/auth/sessions/<int:session_id>",
        type="http",
        auth="public",
        methods=["DELETE"],
        csrf=False,
    )
    def revoke_session(self, session_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, _current_session, auth_mode = self._authenticate()
            self._validate_cookie_csrf(auth_mode)
            target = request.env["brew.api.session"].sudo().search(
                [("id", "=", session_id), ("user_id", "=", user.id), ("revoked_at", "=", False)],
                limit=1,
            )
            if not target:
                raise ApiError("RESOURCE_NOT_FOUND", "会话不存在或已失效。", 404)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            target.action_revoke("user_revoked")
            result = {"revoked": True, "session_id": str(target.id)}
            self._audit(
                "authentication",
                "revoke_session",
                request_id,
                user=user,
                company=company,
                summary={"session_id": target.id},
            )
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)
