from odoo import http
from odoo.http import request

from ..services.planning import PlanningService
from ..services.inventory import InventoryService
from .base import ApiError, BrewApiController


class PlanningController(BrewApiController):
    @http.route("/api/v1/production-plan-packaging-options", type="http", auth="public", methods=["GET"], csrf=False)
    def production_plan_packaging_options(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._planning_context()
            return self._response(service.packaging_options(request.httprequest.args.get("product_tmpl_id"), request.httprequest.args.get("factory_id")), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    def _planning_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation: self._validate_cookie_csrf(mode)
        return user, company, PlanningService(request.env, user, company)

    @http.route("/api/v1/production-plan-inventory-preview", type="http", auth="public", methods=["POST"], csrf=False)
    def inventory_preview(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._planning_context(True)
            return self._response(service.preview_inventory(self._json_body()), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-plan-assignees", type="http", auth="public", methods=["GET"], csrf=False)
    def production_plan_assignees(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._planning_context()
            data = service.assignees(request.httprequest.args.get("factory_id"))
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-plans", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def plans(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._planning_context(mutation)
            if not mutation:
                page = max(int(request.httprequest.args.get("page", 1)), 1); size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
                data, total = service.list(page, size, request.httprequest.args.get("state"))
                return self._response(data, meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total}, request_id=request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            plan, data = service.create(payload)
            self._audit("production_plan", "create", request_id, user=user, company=company, summary={"plan_id": plan.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/production-plans/<int:plan_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def plan(self, plan_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"; user, company, service = self._planning_context(mutation)
            if not mutation: return self._response(service.serialize(service._record(plan_id), True), request_id=request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            plan, data = service.update(plan_id, payload)
            self._audit("production_plan", "update", request_id, user=user, company=company, summary={"plan_id": plan.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/production-plans/<int:plan_id>/progress", type="http", auth="public", methods=["GET"], csrf=False)
    def progress(self, plan_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._planning_context()
            return self._response(service.progress(plan_id), request_id=request_id)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/production-plans/<int:plan_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def planning_action(self, plan_id, action):
        request_id = self._request_id()
        try:
            allowed = {
                "request-stock",
                "complete-stock",
                "set-packaging-plan",
                "set-raw-customer",
                "check-resources",
                "expand-materials",
                "check-inventory",
                "reserve",
                "release-reservations",
                "submit",
                "approve",
                "dispatch",
                "cancel",
                "delete",
                "terminate",
                "complete-brewing",
            }
            if action not in allowed: raise ApiError("RESOURCE_NOT_FOUND", "生产计划动作不存在。", 404)
            if action == "submit": action = "dispatch"
            payload = self._json_body(); user, company, service = self._planning_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            if action in {"check-inventory", "reserve", "release-reservations"}:
                inventory = InventoryService(request.env, user, company)
                if action == "check-inventory":
                    data = inventory.plan_inventory(plan_id, reserve=False)
                elif action == "reserve":
                    data = inventory.plan_inventory(
                        plan_id, reserve=True, expires_at=payload.get("expires_at")
                    )
                else:
                    data = inventory.release_plan_reservations(plan_id)
                plan = service._record(plan_id)
            else:
                plan, data = service.action(plan_id, action, payload)
            if action in {"submit", "dispatch", "complete-stock", "cancel", "delete", "terminate", "complete-brewing"}:
                event_action = "dispatch" if action == "complete-stock" else action
                request.env["brew.outbox.event"].sudo().enqueue(f"production_plan.{event_action}", plan._name, plan.id, {"plan_id": plan.id, "name": plan.name, "state": plan.state}, company)
            self._audit("production_plan", action, request_id, user=user, company=company, summary={"plan_id": plan.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/demands/<int:demand_id>/create-plan", type="http", auth="public", methods=["POST"], csrf=False)
    def create_from_demand(self, demand_id):
        request_id = self._request_id()
        try:
            payload = {**self._json_body(), "demand_id": demand_id}; user, company, service = self._planning_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            plan, data = service.create(payload)
            self._audit("production_plan", "create_from_demand", request_id, user=user, company=company, summary={"plan_id": plan.id, "demand_id": demand_id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc: return self._error_response(exc, request_id)
