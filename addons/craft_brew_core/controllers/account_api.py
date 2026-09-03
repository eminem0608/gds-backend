from odoo import http
from odoo.exceptions import AccessDenied, UserError
from odoo.http import request

from .auth_api import ROLE_XMLIDS, get_json_payload, json_response, serialize_user


class BrewAccountApi(http.Controller):
    def _check_admin(self):
        if not request.env.user.has_group("craft_brew_core.group_brew_admin"):
            raise AccessDenied("Only craft brew admins can manage accounts.")

    def _role_groups(self):
        groups = []
        for xmlid in ROLE_XMLIDS:
            group = request.env.ref(xmlid, raise_if_not_found=False)
            if group:
                groups.append(group)
        return groups

    def _serialize_role(self, group):
        return {"id": group.id, "xmlid": group.get_external_id().get(group.id), "name": group.name}

    def _resolve_role_ids(self, role_xmlids):
        groups = []
        allowed = set(ROLE_XMLIDS)
        for xmlid in role_xmlids or []:
            if xmlid not in allowed:
                raise UserError("角色不在允许范围内")
            group = request.env.ref(xmlid, raise_if_not_found=False)
            if group:
                groups.append(group.id)
        return groups

    @http.route("/api/brew/accounts/roles", type="http", auth="user", methods=["GET"], csrf=False)
    def roles(self, **kwargs):
        self._check_admin()
        return json_response({"success": True, "roles": [self._serialize_role(group) for group in self._role_groups()]})

    @http.route("/api/brew/accounts", type="http", auth="user", methods=["GET"], csrf=False)
    def list_accounts(self, **kwargs):
        self._check_admin()
        users = request.env["res.users"].sudo().search([], order="active desc, name")
        return json_response({"success": True, "accounts": [serialize_user(user) for user in users]})

    @http.route("/api/brew/accounts", type="http", auth="user", methods=["POST"], csrf=False)
    def create_account(self, **kwargs):
        self._check_admin()
        payload = get_json_payload()
        name = payload.get("name")
        login = payload.get("login")
        password = payload.get("password")
        role_ids = self._resolve_role_ids(payload.get("roles"))

        if not name or not login or not password:
            return json_response({"success": False, "message": "姓名、账号、密码不能为空"}, status=400)

        base_user_group = request.env.ref("base.group_user").id
        user = request.env["res.users"].sudo().create(
            {
                "name": name,
                "login": login,
                "password": password,
                "groups_id": [(6, 0, [base_user_group] + role_ids)],
            }
        )
        return json_response({"success": True, "account": serialize_user(user)})

    @http.route("/api/brew/accounts/<int:user_id>", type="http", auth="user", methods=["PUT"], csrf=False)
    def update_account(self, user_id, **kwargs):
        self._check_admin()
        payload = get_json_payload()
        user = request.env["res.users"].sudo().browse(user_id)
        if not user.exists():
            return json_response({"success": False, "message": "账号不存在"}, status=404)

        values = {}
        for field_name in ["name", "login", "active"]:
            if field_name in payload:
                values[field_name] = payload[field_name]
        if payload.get("password"):
            values["password"] = payload["password"]
        if "roles" in payload:
            current_role_ids = [group.id for group in self._role_groups()]
            target_role_ids = self._resolve_role_ids(payload.get("roles"))
            keep_group_ids = [group.id for group in user.groups_id if group.id not in current_role_ids]
            values["groups_id"] = [(6, 0, keep_group_ids + target_role_ids)]

        user.write(values)
        return json_response({"success": True, "account": serialize_user(user)})
