from odoo import http
from odoo.http import request

from ..services.demand import DemandService
from .base import ApiError, BrewApiController


class DemandController(BrewApiController):
    def _demand_context(self, mutation=False):
        user, company, _session, auth_mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, DemandService(request.env, user, company)

    @http.route("/api/v1/demands", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def demands(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._demand_context(mutation)
            if not mutation:
                page = max(int(request.httprequest.args.get("page", 1)), 1)
                page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
                data, total = service.list(page, page_size, request.httprequest.args.get("state"), request.httprequest.args.get("keyword", "").strip())
                return self._response(data, meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total}, request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            demand, data = service.create(payload)
            self._audit("demand", "create", request_id, user=user, company=company, summary={"demand_id": demand.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/demands/<int:demand_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def demand(self, demand_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._demand_context(mutation)
            if not mutation:
                return self._response(service.serialize(service._record(demand_id)), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            demand, data = service.update(demand_id, payload)
            self._audit("demand", "update", request_id, user=user, company=company, summary={"demand_id": demand.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/demands/<int:demand_id>/<string:action>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def demand_action(self, demand_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "confirm", "return", "cancel"}:
                raise ApiError("RESOURCE_NOT_FOUND", "产品需求动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._demand_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            demand, data = service.action(demand_id, action, payload)
            request.env["brew.outbox.event"].sudo().enqueue(
                f"product_demand.{action}", "brew.product.demand", demand.id,
                {"demand_id": demand.id, "name": demand.name, "state": demand.state}, company,
            )
            self._audit("demand", action, request_id, user=user, company=company, summary={"demand_id": demand.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
