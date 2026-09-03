import json

from odoo import http
from odoo.http import request


ROLE_XMLIDS = [
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_operator",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_purchase",
    "craft_brew_core.group_brew_sales",
    "craft_brew_core.group_brew_finance",
    "craft_brew_core.group_brew_admin",
]


def json_response(payload, status=200):
    return request.make_response(
        json.dumps(payload, ensure_ascii=False),
        headers=[("Content-Type", "application/json; charset=utf-8")],
        status=status,
    )


def get_json_payload():
    return request.httprequest.get_json(silent=True) or {}


def serialize_user(user):
    role_groups = []
    for xmlid in ROLE_XMLIDS:
        group = request.env.ref(xmlid, raise_if_not_found=False)
        if group and group in user.groups_id:
            role_groups.append(
                {
                    "id": group.id,
                    "xmlid": xmlid,
                    "name": group.name,
                }
            )

    return {
        "id": user.id,
        "name": user.name,
        "login": user.login,
        "active": user.active,
        "roles": role_groups,
        "is_admin": user.has_group("craft_brew_core.group_brew_admin"),
    }


class BrewAuthApi(http.Controller):
    @http.route("/api/brew/auth/login", type="http", auth="none", methods=["POST"], csrf=False)
    def login(self, **kwargs):
        payload = get_json_payload()
        login = payload.get("login")
        password = payload.get("password")
        db = payload.get("db") or request.session.db or request.db

        if not login or not password:
            return json_response({"success": False, "message": "账号和密码不能为空"}, status=400)

        try:
            uid = request.session.authenticate(db, login, password)
        except Exception:
            uid = False

        if not uid:
            return json_response({"success": False, "message": "账号或密码错误"}, status=401)

        user = request.env["res.users"].sudo().browse(uid)
        return json_response({"success": True, "user": serialize_user(user)})

    @http.route("/api/brew/auth/me", type="http", auth="user", methods=["GET"], csrf=False)
    def me(self, **kwargs):
        return json_response({"success": True, "user": serialize_user(request.env.user)})

    @http.route("/api/brew/auth/logout", type="http", auth="user", methods=["POST"], csrf=False)
    def logout(self, **kwargs):
        request.session.logout(keep_db=True)
        return json_response({"success": True})
