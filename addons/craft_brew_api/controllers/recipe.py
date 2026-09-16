from odoo import http
from odoo.http import request

from ..services.recipe import RecipeService
from .base import ApiError, BrewApiController


class RecipeController(BrewApiController):
    def _context(self, mutation=False):
        user, company, _session, auth_mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, RecipeService(request.env, user, company)

    def _page_args(self):
        page = max(int(request.httprequest.args.get("page", 1)), 1)
        page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
        return page, page_size

    @http.route("/api/v1/recipes", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def recipes(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._context(mutation)
            if not mutation:
                page, page_size = self._page_args()
                active_arg = request.httprequest.args.get("active")
                active = None if active_arg is None else active_arg.lower() in {"1", "true", "yes"}
                data, total = service.list_recipes(page, page_size, request.httprequest.args.get("keyword", "").strip(), active)
                return self._response(data, meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total}, request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            recipe, data = service.create_recipe(payload)
            self._audit("recipe", "create", request_id, user=user, company=company, summary={"recipe_id": recipe.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/recipes/<int:recipe_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def recipe(self, recipe_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._context(mutation)
            if not mutation:
                return self._response(service.serialize_recipe(service._recipe(recipe_id)), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            recipe, data = service.update_recipe(recipe_id, payload)
            self._audit("recipe", "update", request_id, user=user, company=company, summary={"recipe_id": recipe.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/recipes/<int:recipe_id>/versions", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def versions(self, recipe_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._context(mutation)
            if not mutation:
                return self._response(service.list_versions(recipe_id), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            version, data = service.create_version(recipe_id, payload)
            self._audit("recipe_version", "create", request_id, user=user, company=company, summary={"recipe_id": recipe_id, "version_id": version.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/recipe-versions/<int:version_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def version(self, version_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._context(mutation)
            if not mutation:
                return self._response(service.serialize_version(service._version(version_id), True), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            version, data = service.update_version(version_id, payload)
            self._audit("recipe_version", "update", request_id, user=user, company=company, summary={"version_id": version.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        [
            "/api/v1/recipe-versions/<int:version_id>/materials",
            "/api/v1/recipe-versions/<int:version_id>/process-steps",
        ],
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
    )
    def replace_lines(self, version_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            if request.httprequest.path.endswith("/materials"):
                version, data = service.replace_materials(version_id, payload)
                action = "replace_materials"
            else:
                version, data = service.replace_steps(version_id, payload)
                action = "replace_process_steps"
            self._audit("recipe_version", action, request_id, user=user, company=company, summary={"version_id": version.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/recipe-versions/<int:version_id>/calculate", type="http", auth="public", methods=["POST"], csrf=False)
    def calculate(self, version_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            data = service.calculate(version_id, payload.get("volume_l"))
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/recipe-versions/<int:version_id>/copy", type="http", auth="public", methods=["POST"], csrf=False)
    def copy_version(self, version_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            version, data = service.copy_version(version_id)
            self._audit("recipe_version", "copy", request_id, user=user, company=company, summary={"source_version_id": version_id, "version_id": version.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/recipe-versions/<int:version_id>/<string:action>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def version_action(self, version_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "approve", "return", "activate", "deactivate"}:
                raise ApiError("RESOURCE_NOT_FOUND", "配方版本动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            version, data = service.action(version_id, action, payload)
            if action in {"activate", "deactivate"}:
                request.env["brew.outbox.event"].sudo().enqueue(
                    f"recipe.version.{action}", "brew.recipe.version", version.id,
                    {"recipe_id": version.recipe_id.id, "version_id": version.id, "version": version.version}, company,
                )
            self._audit("recipe_version", action, request_id, user=user, company=company, summary={"version_id": version.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/recipe-versions/<int:version_id>/compare/<int:other_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def compare(self, version_id, other_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            return self._response(service.compare(version_id, other_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)
