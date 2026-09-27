import json

from odoo import Command, http
from odoo.http import request

from odoo.addons.craft_brew_core.services.role_service import (
    MENU_DEFINITIONS,
    ROLE_DEFINITIONS,
    menu_codes_for_role,
)

from .base import ApiError, BrewApiController


class AdminController(BrewApiController):
    def _managed_user_domain(self, company):
        system_user_ids = [
            user.id
            for xml_id in (
                "base.user_root",
                "base.default_user",
                "base.public_user",
                "base.template_portal_user_id",
            )
            if (user := request.env.ref(xml_id, raise_if_not_found=False))
        ]
        return [
            ("company_ids", "in", [company.id]),
            ("id", "not in", system_user_ids),
        ]

    def _managed_users(self, company):
        return request.env["res.users"].sudo().with_context(active_test=False).search(
            self._managed_user_domain(company)
        )

    def _admin_context(self, mutation=False):
        user, company, session, auth_mode = self._authenticate()
        self._require_group(user, "craft_brew_core.group_brew_admin")
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, session

    def _serialize_user(self, user):
        direct_codes = user.brew_direct_role_code_list()
        effective_codes = user.brew_role_codes()
        return {
            "id": str(user.id),
            "name": user.name,
            "login": user.login,
            "active": user.active,
            "company_ids": [str(company.id) for company in user.company_ids],
            "role_codes": direct_codes,
            "effective_role_codes": effective_codes,
            "inherited_role_codes": [code for code in effective_codes if code not in direct_codes],
            "organization_id": str(user.brew_organization_id.id) if user.brew_organization_id else None,
            "organization_name": user.brew_organization_id.display_name if user.brew_organization_id else None,
            "job_title": user.brew_job_title or "",
            "is_organization_manager": user.brew_is_organization_manager,
            "factory_ids": [str(factory.id) for factory in user.brew_factory_ids],
            "default_factory_id": str(user.brew_default_factory_id.id) if user.brew_default_factory_id else None,
            "supplier_id": str(user.brew_supplier_id.id) if user.brew_supplier_id else None,
            "supplier_name": user.brew_supplier_id.name if user.brew_supplier_id else None,
            "version": user.write_date.isoformat() if user.write_date else None,
        }

    def _ensure_organizations(self, company):
        model = request.env["brew.organization"].sudo().with_context(active_test=False)
        root = model.search(
            [
                ("company_id", "=", company.id),
                ("organization_type", "=", "company"),
                ("parent_id", "=", False),
            ],
            limit=1,
        )
        if not root:
            root = model.create(
                {
                    "company_id": company.id,
                    "name": company.name,
                    "code": f"ORG-{company.id}",
                    "organization_type": "company",
                    "sequence": 1,
                }
            )
        factories = request.env["brew.factory"].sudo().with_context(active_test=False).search(
            [("company_id", "=", company.id)]
        )
        for factory in factories:
            code = f"FACTORY-{factory.id}"
            organization = model.search(
                [("company_id", "=", company.id), ("code", "=", code)], limit=1
            )
            values = {
                "name": factory.name,
                "parent_id": root.id,
                "organization_type": "factory",
                "address": factory.address or "",
                "active": factory.active,
            }
            if organization:
                organization.write(values)
            else:
                model.create({"company_id": company.id, "code": code, **values})
        return model.search([("company_id", "=", company.id)], order="sequence,name,id")

    def _serialize_organization(self, organization, user_counts=None, role_names=None):
        user_counts = user_counts or {}
        role_names = role_names or {}
        return {
            "id": str(organization.id),
            "name": organization.name,
            "code": organization.code,
            "parent_id": str(organization.parent_id.id) if organization.parent_id else None,
            "path": organization.display_name,
            "type": organization.organization_type,
            "address": organization.address or "",
            "sequence": organization.sequence,
            "active": organization.active,
            "user_count": user_counts.get(organization.id, 0),
            "role_names": role_names.get(organization.id, []),
            "version": organization.write_date.isoformat() if organization.write_date else None,
        }

    def _role_configurations(self, company):
        return request.env["brew.role.configuration"].ensure_company_roles(company)

    def _serialize_role(self, role, users):
        direct_users = users.filtered(lambda item: role.code in item.brew_direct_role_code_list())
        effective_users = users.filtered(lambda item: role.code in item.brew_role_codes())
        return {
            "id": str(role.id),
            "code": role.code,
            "label": role.name,
            "description": role.description or "",
            "active": role.active,
            "default_page": ROLE_DEFINITIONS[role.code]["default_page"],
            "permissions": sorted(ROLE_DEFINITIONS[role.code]["permissions"]),
            "menu_codes": role.menu_codes if role.menu_codes is not None else menu_codes_for_role(role.code),
            "organization_ids": [str(item.id) for item in role.organization_ids],
            "organization_names": role.organization_ids.mapped("name"),
            "direct_user_ids": [str(item.id) for item in direct_users],
            "direct_user_names": direct_users.mapped("name"),
            "effective_user_count": len(effective_users),
            "version": role.write_date.isoformat() if role.write_date else None,
        }

    def _user_in_company(self, user_id, company):
        user = request.env["res.users"].sudo().search(
            [("id", "=", int(user_id)), *self._managed_user_domain(company)], limit=1
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
            _actor, company, _session = self._admin_context()
            roles = self._role_configurations(company)
            users = self._managed_users(company)
            return self._response(
                [self._serialize_role(role, users) for role in roles],
                meta={
                    "menus": [
                        {"code": code, "label": label}
                        for code, label, _allowed_roles in MENU_DEFINITIONS
                    ]
                },
                request_id=request_id,
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/roles/<string:role_code>", type="http", auth="public", methods=["PATCH"], csrf=False)
    def update_role(self, role_code):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            roles = self._role_configurations(company)
            role = roles.filtered(lambda item: item.code == role_code)[:1]
            if not role:
                raise ApiError("RESOURCE_NOT_FOUND", "角色不存在。", 404)
            self._check_version(role, payload)
            values = {}
            if "name" in payload:
                values["name"] = str(payload.get("name") or "").strip()
                if not values["name"]:
                    raise ApiError("VALIDATION_REQUIRED", "角色名称不能为空。", 422)
            if "description" in payload:
                values["description"] = str(payload.get("description") or "").strip()
            if "active" in payload:
                if role.code == "tenant_admin" and not payload.get("active"):
                    raise ApiError("STATE_ACTION_NOT_ALLOWED", "系统管理员角色不能停用。", 422)
                values["active"] = bool(payload.get("active"))
            if "menu_codes" in payload:
                menu_codes = list(dict.fromkeys(payload.get("menu_codes") or []))
                valid_codes = {code for code, _label, _allowed in MENU_DEFINITIONS}
                if set(menu_codes) - valid_codes:
                    raise ApiError("VALIDATION_FORMAT", "存在未知菜单编码。", 422)
                values["menu_codes"] = menu_codes
            if "organization_ids" in payload:
                requested_ids = {int(item) for item in payload.get("organization_ids") or []}
                organizations = request.env["brew.organization"].sudo().search(
                    [("id", "in", list(requested_ids)), ("company_id", "=", company.id)]
                )
                if set(organizations.ids) != requested_ids:
                    raise ApiError("DATA_SCOPE_DENIED", "存在不属于当前企业的组织。", 403)
                values["organization_ids"] = [Command.set(organizations.ids)]
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            if values:
                role.write(values)
            users = self._managed_users(company)
            if "direct_user_ids" in payload:
                requested_ids = {int(item) for item in payload.get("direct_user_ids") or []}
                if set(users.filtered(lambda item: item.id in requested_ids).ids) != requested_ids:
                    raise ApiError("DATA_SCOPE_DENIED", "存在不属于当前企业的用户。", 403)
                if role.code == "tenant_admin" and actor.id not in requested_ids:
                    raise ApiError("STATE_ACTION_NOT_ALLOWED", "不能移除当前账号的系统管理员角色。", 422)
                for user in users:
                    direct_codes = set(user.brew_direct_role_code_list())
                    if user.id in requested_ids:
                        direct_codes.add(role.code)
                    else:
                        direct_codes.discard(role.code)
                    user.write({"brew_direct_role_codes": sorted(direct_codes)})
            users._sync_brew_security_groups()
            result = self._serialize_role(role, users)
            self._audit("role", "update", request_id, user=actor, company=company, summary={"role_code": role.code})
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/organizations", type="http", auth="public", methods=["GET"], csrf=False)
    def organizations(self):
        request_id = self._request_id()
        try:
            _actor, company, _session = self._admin_context()
            organizations = self._ensure_organizations(company)
            users = self._managed_users(company)
            user_counts = {
                organization.id: len(users.filtered(lambda item: item.brew_organization_id == organization))
                for organization in organizations
            }
            role_names = {}
            for role in self._role_configurations(company):
                for organization in role.organization_ids:
                    role_names.setdefault(organization.id, []).append(role.name)
            return self._response(
                [self._serialize_organization(item, user_counts, role_names) for item in organizations],
                request_id=request_id,
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/organizations", type="http", auth="public", methods=["POST"], csrf=False)
    def create_organization(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            name = str(payload.get("name") or "").strip()
            code = str(payload.get("code") or "").strip().upper()
            if not name or not code:
                raise ApiError("VALIDATION_REQUIRED", "组织名称和编码不能为空。", 422)
            organization_type = payload.get("type") or "department"
            if organization_type not in {"company", "factory", "department", "workshop", "team"}:
                raise ApiError("VALIDATION_FORMAT", "组织类型无效。", 422)
            if request.env["brew.organization"].sudo().with_context(active_test=False).search_count(
                [("company_id", "=", company.id), ("code", "=", code)]
            ):
                raise ApiError("VALIDATION_DUPLICATE", "组织编码已存在。", 422)
            parent_id = int(payload.get("parent_id") or 0)
            parent = request.env["brew.organization"].sudo().search(
                [("id", "=", parent_id), ("company_id", "=", company.id)], limit=1
            ) if parent_id else request.env["brew.organization"]
            if parent_id and not parent:
                raise ApiError("DATA_SCOPE_DENIED", "上级组织不存在。", 403)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            organization = request.env["brew.organization"].sudo().create(
                {
                    "company_id": company.id,
                    "name": name,
                    "code": code,
                    "parent_id": parent.id if parent else False,
                    "organization_type": organization_type,
                    "address": str(payload.get("address") or "").strip(),
                    "sequence": int(payload.get("sequence") or 10),
                }
            )
            result = self._serialize_organization(organization)
            self._audit("organization", "create", request_id, user=actor, company=company, summary={"organization_id": organization.id})
            return self._complete_idempotency(idem, result, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/organizations/<int:organization_id>", type="http", auth="public", methods=["PATCH"], csrf=False)
    def update_organization(self, organization_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            actor, company, _session = self._admin_context(mutation=True)
            organization = request.env["brew.organization"].sudo().with_context(active_test=False).search(
                [("id", "=", organization_id), ("company_id", "=", company.id)], limit=1
            )
            if not organization:
                raise ApiError("RESOURCE_NOT_FOUND", "组织不存在。", 404)
            self._check_version(organization, payload)
            values = {}
            if "name" in payload:
                values["name"] = str(payload.get("name") or "").strip()
                if not values["name"]:
                    raise ApiError("VALIDATION_REQUIRED", "组织名称不能为空。", 422)
            if "code" in payload:
                values["code"] = str(payload.get("code") or "").strip().upper()
                if not values["code"]:
                    raise ApiError("VALIDATION_REQUIRED", "组织编码不能为空。", 422)
                duplicate = request.env["brew.organization"].sudo().with_context(active_test=False).search_count(
                    [
                        ("company_id", "=", company.id),
                        ("code", "=", values["code"]),
                        ("id", "!=", organization.id),
                    ]
                )
                if duplicate:
                    raise ApiError("VALIDATION_DUPLICATE", "组织编码已存在。", 422)
            if "type" in payload:
                if payload.get("type") not in {"company", "factory", "department", "workshop", "team"}:
                    raise ApiError("VALIDATION_FORMAT", "组织类型无效。", 422)
                values["organization_type"] = payload["type"]
            if "address" in payload:
                values["address"] = str(payload.get("address") or "").strip()
            if "active" in payload:
                values["active"] = bool(payload.get("active"))
            if "sequence" in payload:
                values["sequence"] = int(payload.get("sequence") or 10)
            if "parent_id" in payload:
                parent_id = int(payload.get("parent_id") or 0)
                if parent_id == organization.id:
                    raise ApiError("VALIDATION_FORMAT", "组织不能选择自己作为上级。", 422)
                descendant_ids = request.env["brew.organization"].sudo().with_context(active_test=False).search(
                    [("id", "child_of", organization.id)]
                ).ids
                if parent_id in descendant_ids:
                    raise ApiError("VALIDATION_FORMAT", "不能将下级组织设为上级。", 422)
                parent = request.env["brew.organization"].sudo().search(
                    [("id", "=", parent_id), ("company_id", "=", company.id)], limit=1
                ) if parent_id else request.env["brew.organization"]
                if parent_id and not parent:
                    raise ApiError("DATA_SCOPE_DENIED", "上级组织不存在。", 403)
                values["parent_id"] = parent.id if parent else False
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            organization.write(values)
            result = self._serialize_organization(organization)
            self._audit("organization", "update", request_id, user=actor, company=company, summary={"organization_id": organization.id})
            return self._complete_idempotency(idem, result)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/users", type="http", auth="public", methods=["GET"], csrf=False)
    def users(self, **_kwargs):
        request_id = self._request_id()
        try:
            _actor, company, _session = self._admin_context()
            page = max(int(request.httprequest.args.get("page", 1)), 1)
            page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
            keyword = request.httprequest.args.get("keyword", "").strip()
            domain = self._managed_user_domain(company)
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
            organization_id = payload.get("organization_id")
            organization = request.env["brew.organization"].sudo().search(
                [("id", "=", int(organization_id or 0)), ("company_id", "=", company.id)], limit=1
            ) if organization_id else request.env["brew.organization"]
            if organization_id and not organization:
                raise ApiError("DATA_SCOPE_DENIED", "所属组织不存在。", 403)
            supplier_id = payload.get("supplier_id")
            supplier = request.env["res.partner"].sudo().search(
                [("id", "=", int(supplier_id or 0)), ("company_id", "=", company.id), ("supplier_rank", ">", 0)], limit=1
            ) if supplier_id else request.env["res.partner"]
            if "supplier" in (payload.get("role_codes") or []) and not supplier:
                raise ApiError("VALIDATION_REQUIRED", "供应商账号必须绑定供应商主体。", 422)
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
                    "brew_supplier_id": supplier.id if supplier else False,
                    "brew_organization_id": organization.id if organization else False,
                    "brew_job_title": str(payload.get("job_title") or "").strip(),
                    "brew_is_organization_manager": bool(payload.get("is_organization_manager")),
                    "brew_direct_role_codes": list(dict.fromkeys(payload.get("role_codes") or [])),
                }
            )
            user._sync_brew_security_groups()
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
            if "organization_id" in payload:
                organization_id = payload.get("organization_id")
                organization = request.env["brew.organization"].sudo().search(
                    [("id", "=", int(organization_id or 0)), ("company_id", "=", company.id)], limit=1
                ) if organization_id else request.env["brew.organization"]
                if organization_id and not organization:
                    raise ApiError("DATA_SCOPE_DENIED", "所属组织不存在。", 403)
                values["brew_organization_id"] = organization.id if organization else False
            if "job_title" in payload:
                values["brew_job_title"] = str(payload.get("job_title") or "").strip()
            if "is_organization_manager" in payload:
                values["brew_is_organization_manager"] = bool(payload.get("is_organization_manager"))
            if "supplier_id" in payload:
                supplier_id = payload.get("supplier_id")
                supplier = request.env["res.partner"].sudo().search(
                    [
                        ("id", "=", int(supplier_id or 0)),
                        ("company_id", "=", company.id),
                        ("supplier_rank", ">", 0),
                    ],
                    limit=1,
                ) if supplier_id else request.env["res.partner"]
                if supplier_id and not supplier:
                    raise ApiError("DATA_SCOPE_DENIED", "关联供应商不存在。", 403)
                values["brew_supplier_id"] = supplier.id if supplier else False
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
            role_codes = list(dict.fromkeys(payload.get("role_codes") or []))
            role_ids = set(self._role_group_ids(role_codes))
            if target == actor and "tenant_admin" not in role_codes:
                raise ApiError("STATE_ACTION_NOT_ALLOWED", "不能移除当前账号的系统管理员角色。", 422)
            if "supplier" in role_codes and not target.brew_supplier_id:
                raise ApiError("VALIDATION_REQUIRED", "供应商账号必须绑定供应商主体。", 422)
            idem, replay = self._start_idempotency(actor, company, payload)
            if replay:
                return replay
            all_brew_role_ids = {
                request.env.ref(definition["xml_id"]).id for definition in ROLE_DEFINITIONS.values()
            }
            retained_ids = set(target.groups_id.ids) - all_brew_role_ids
            retained_ids.add(request.env.ref("base.group_user").id)
            target.write(
                {
                    "brew_direct_role_codes": role_codes,
                    "groups_id": [Command.set(sorted(retained_ids | role_ids))],
                }
            )
            target._sync_brew_security_groups()
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
