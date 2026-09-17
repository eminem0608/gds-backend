from odoo import http
from odoo.http import request

from ..services.trace import TraceService
from .base import ApiError, BrewApiController


class TraceController(BrewApiController):
    def _trace_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, TraceService(request.env, user, company)

    @staticmethod
    def _trace_paging():
        return max(int(request.httprequest.args.get("page", 1)), 1), min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)

    def _trace_page(self, data, total, page, size, request_id):
        return self._response(data, meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total}, request_id=request_id)

    def _trace_command(self, event_type, action, handler, status=200):
        request_id = self._request_id()
        try:
            payload = self._json_body(); user, company, service = self._trace_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = handler(service, payload)
            self._audit(event_type, action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, status)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/packaging-orders", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def packaging_orders(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"; user, company, service = self._trace_context(mutation)
            if not mutation:
                page, size = self._trace_paging(); data, total = service.packaging_orders(page, size, request.httprequest.args.get("state"))
                return self._trace_page(data, total, page, size, request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = service.create_packaging_order(payload)
            self._audit("packaging_order", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/packaging-orders/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def packaging_action(self, record_id, action):
        if action not in {"start", "submit", "release", "cancel", "generate-codes"}:
            return self._error_response(ApiError("RESOURCE_NOT_FOUND", "Packaging action does not exist.", 404), self._request_id())
        return self._trace_command(
            "packaging_order", action,
            (lambda service, payload: service.generate_codes(record_id, payload)) if action == "generate-codes" else (lambda service, payload: service.packaging_action(record_id, action, payload)),
        )

    @http.route("/api/v1/trace-codes", type="http", auth="public", methods=["GET"], csrf=False)
    def trace_codes(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._trace_context(); page, size = self._trace_paging()
            filters = {key: request.httprequest.args.get(key) for key in ["code_type", "state", "lot_id", "q"] if request.httprequest.args.get(key)}
            data, total = service.codes(page, size, filters)
            return self._trace_page(data, total, page, size, request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/outbound-orders", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def outbound_orders(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"; user, company, service = self._trace_context(mutation)
            if not mutation:
                page, size = self._trace_paging(); data, total = service.outbound_orders(page, size, request.httprequest.args.get("state"))
                return self._trace_page(data, total, page, size, request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = service.create_outbound(payload)
            self._audit("outbound_order", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/outbound-orders/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def outbound_action(self, record_id, action):
        if action not in {"ship", "cancel"}:
            return self._error_response(ApiError("RESOURCE_NOT_FOUND", "Outbound action does not exist.", 404), self._request_id())
        return self._trace_command("outbound_order", action, lambda service, payload: service.outbound_action(record_id, action, payload))

    @http.route("/api/v1/trace/<string:code>", type="http", auth="public", methods=["GET"], csrf=False)
    def trace(self, code):
        request_id = self._request_id()
        try:
            _user, _company, service = self._trace_context()
            return self._response(service.trace_code(code), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/trace/lots/<int:lot_id>/forward", type="http", auth="public", methods=["GET"], csrf=False)
    def forward_trace(self, lot_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._trace_context()
            return self._response(service.forward_lot(lot_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)
