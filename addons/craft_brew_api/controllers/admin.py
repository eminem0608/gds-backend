import json

from odoo import Command, http
from odoo.http import request

from odoo.addons.craft_brew_core.services.role_service import ROLE_DEFINITIONS

from .base import ApiError, BrewApiController


class AdminController(BrewApiController):
    def _admin_context(self, mutation=False):
        user, company, session, auth_mode = self._authenticate()
        self._require_group(user, "craft_brew_core.group_brew_admin")
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, session

    def _serialize_user(self, user):
        return {
            "id": str(user.id),
            "name": user.name,
            "login": user.login,
            "active": user.active,
            "company_ids": [str(company.id) for company in user.company_ids],
            "role_codes": user.brew_role_codes(),
            "factory_ids": [str(factory.id) for factory in user.brew_factory_ids],
            "default_factory_id": str(user.brew_default_factory_id.id) if user.brew_default_factory_id else None,
            "version": user.write_date.isoformat() if user.write_date else None,
        }

    def _user_in_company(self, user_id, company):
        user = request.env["res.users"].sudo().search(
            [("id", "=", int(user_id)), ("company_ids", "in", [company.id])], limit=1
        )
        if not user:
            raise ApiError("RESOURCE_NOT_FOUND", "用户不存在。", 404)
        return user

    def _check_version(self, record, payload):
        expected = payload.get("version")
        current = record.write_date.isoformat() if record.write_date else None
        if expected and expected != current:
            raise ApiError(
                "VERSION_CONFLICT",
                "数据已被其他用户修改，请刷新后重试。",
                409,
                {"current_version": current},
            )

    def _role_group_ids(self, role_codes):
        invalid = sorted(set(role_codes) - set(ROLE_DEFINITIONS))
        if invalid:
            raise ApiError("VALIDATION_FORMAT", "存在未知角色编码。", 422, {"role_codes": invalid})
        return [request.env.ref(ROLE_DEFINITIONS[code]["xml_id"]).id for code in role_codes]

    @http.route("/api/v1/admin/roles", type="http", auth="public", methods=["GET"], csrf=False)
    def roles(self):
        request_id = self._request_id()
        try:
            self._admin_context()
            data = [
                {
                    "code": code,
                    "label": definition["label"],
                    "default_page": definition["default_page"],
                    "permissions": sorted(definition["permissions"]),
                }
                for code, definition in ROLE_DEFINITIONS.items()
            ]
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users", type="http", auth="public", methods=["GET"], csrf=False)
    def users(self):
        request_id = self._request_id()
        try:
            _actor, company, _session = self._admin_context()
            page = max(int(request.httprequest.args.get("page", 1)), 1)
            page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
            keyword = request.httprequest.args.get("keyword", "").strip()
            domain = [("company_ids", "in", [company.id])]
            if keyword:
                domain += ["|", ("name", "ilike", keyword), ("login", "ilike", keyword)]
            model = request.env["res.users"].sudo().with_context(active_test=False)
            total = model.search_count(domain)
            records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="name,id")
            return self._response(
                [self._serialize_user(item) for item in records],
                meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total},
                request_id=request_id,
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users", type="http", auth="public", methods=["POST"], csrf=False)
    def create_user(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            name = str(payload.get("name") or "").strip()
            login = str(payload.get("login") or "").strip()
            password = str(payload.get("password") or "")
            if not name or not login or len(password) < 8:
                raise ApiError("VALIDATION_REQUIRED", "姓名、账号必填，初始密码至少 8 位。", 422)
            if request.env["res.users"].sudo().with_context(active_test=False).search_count([("login", "=", login)]):
                raise ApiError("VALIDATION_DUPLICATE", "账号已存在。", 422)
            role_group_ids = self._role_group_ids(payload.get("role_codes") or [])
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            internal_group = request.env.ref("base.group_user")
            user = request.env["res.users"].sudo().with_context(no_reset_password=True).create(
                {
                    "name": name,
                    "login": login,
                    "password": password,
                    "company_id": company.id,
                    "company_ids": [Command.set([company.id])],
                    "groups_id": [Command.set([internal_group.id, *role_group_ids])],
                }
            )
            result = self._serialize_user(user)
            self._audit("user", "create", request_id, user=actor, company=company, summary={"target_user_id": user.id})
            return self._complete_idempotency(idem, result, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users/<int:user_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def user_detail(self, user_id):
        request_id = self._request_id()
        try:
            _actor, company, _session = self._admin_context()
            return self._response(self._serialize_user(self._user_in_company(user_id, company)), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users/<int:user_id>", type="http", auth="public", methods=["PATCH"], csrf=False)
    def update_user(self, user_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            target = self._user_in_company(user_id, company)
            self._check_version(target, payload)
            values = {}
            if "name" in payload:
                values["name"] = str(payload.get("name") or "").strip()
                if not values["name"]:
                    raise ApiError("VALIDATION_REQUIRED", "姓名不能为空。", 422)
            if "login" in payload:
                values["login"] = str(payload.get("login") or "").strip()
                if not values["login"]:
                    raise ApiError("VALIDATION_REQUIRED", "账号不能为空。", 422)
                duplicate = request.env["res.users"].sudo().with_context(active_test=False).search_count(
                    [("login", "=", values["login"]), ("id", "!=", target.id)]
                )
                if duplicate:
                    raise ApiError("VALIDATION_DUPLICATE", "账号已存在。", 422)
            if not values:
                raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            target.write(values)
            result = self._serialize_user(target)
            self._audit(
                "user",
                "update",
                request_id,
                user=actor,
                company=company,
                summary={"target_user_id": target.id, "fields": sorted(values)},
            )
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users/<int:user_id>/roles", type="http", auth="public", methods=["PUT"], csrf=False)
    def update_roles(self, user_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            target = self._user_in_company(user_id, company)
            self._check_version(target, payload)
            role_ids = set(self._role_group_ids(payload.get("role_codes") or []))
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            all_brew_role_ids = {
                request.env.ref(definition["xml_id"]).id for definition in ROLE_DEFINITIONS.values()
            }
            retained_ids = set(target.groups_id.ids) - all_brew_role_ids
            retained_ids.add(request.env.ref("base.group_user").id)
            target.write({"groups_id": [Command.set(sorted(retained_ids | role_ids))]})
            result = self._serialize_user(target)
            self._audit("user", "assign_roles", request_id, user=actor, company=company, summary={"target_user_id": target.id, "role_codes": payload.get("role_codes") or []})
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users/<int:user_id>/data-scopes", type="http", auth="public", methods=["PUT"], csrf=False)
    def update_data_scopes(self, user_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            target = self._user_in_company(user_id, company)
            self._check_version(target, payload)
            requested_ids = {int(item) for item in payload.get("factory_ids") or []}
            factories = request.env["brew.factory"].sudo().search(
                [("id", "in", list(requested_ids)), ("company_id", "=", company.id)]
            )
            if set(factories.ids) != requested_ids:
                raise ApiError("DATA_SCOPE_DENIED", "存在不属于当前企业的厂区。", 403)
            retained = target.brew_factory_ids.filtered(lambda item: item.company_id != company)
            all_factories = retained | factories
            default_factory_id = payload.get("default_factory_id")
            if default_factory_id and int(default_factory_id) not in factories.ids:
                raise ApiError("VALIDATION_FORMAT", "默认厂区必须在本次授权范围内。", 422)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            target.write(
                {
                    "brew_factory_ids": [Command.set(all_factories.ids)],
                    "brew_default_factory_id": int(default_factory_id) if default_factory_id else False,
                }
            )
            result = self._serialize_user(target)
            self._audit("user", "assign_data_scopes", request_id, user=actor, company=company, summary={"target_user_id": target.id, "factory_ids": factories.ids})
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users/<int:user_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def change_user_state(self, user_id, action):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            if action not in {"enable", "disable"}:
                raise ApiError("RESOURCE_NOT_FOUND", "操作不存在。", 404)
            if action == "disable" and actor.id == user_id:
                raise ApiError("STATE_ACTION_NOT_ALLOWED", "不能停用当前登录账号。", 422)
            target = self._user_in_company(user_id, company)
            self._check_version(target, payload)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            target.write({"active": action == "enable"})
            result = self._serialize_user(target)
            self._audit("user", action, request_id, user=actor, company=company, summary={"target_user_id": target.id})
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/admin/users/<int:user_id>/reset-password",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def reset_password(self, user_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            target = self._user_in_company(user_id, company)
            password = str(payload.get("password") or "")
            if len(password) < 8:
                raise ApiError("VALIDATION_FORMAT", "新密码至少 8 位。", 422)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            target.write(
                {
                    "password": password,
                    "brew_session_version": target.brew_session_version + 1,
                }
            )
            target.brew_api_session_ids.action_revoke("password_reset")
            result = {"password_reset": True, "user_id": str(target.id)}
            self._audit(
                "user",
                "reset_password",
                request_id,
                user=actor,
                company=company,
                summary={"target_user_id": target.id},
            )
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/audit-logs", type="http", auth="public", methods=["GET"], csrf=False)
    def audit_logs(self):
        request_id = self._request_id()
        try:
            _actor, company, _session = self._admin_context()
            page = max(int(request.httprequest.args.get("page", 1)), 1)
            page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
            model = request.env["brew.audit.log"].sudo()
            domain = [("company_id", "=", company.id)]
            total = model.search_count(domain)
            records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
            data = [
                {
                    "id": str(item.id),
                    "event_type": item.event_type,
                    "action": item.action,
                    "user": {"id": str(item.user_id.id), "name": item.user_id.name} if item.user_id else None,
                    "resource_model": item.resource_model,
                    "resource_id": item.resource_id,
                    "request_id": item.request_id,
                    "summary": json.loads(item.summary_json or "{}"),
                    "created_at": item.create_date.isoformat() if item.create_date else None,
                }
                for item in records
            ]
            return self._response(data, meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total}, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)
