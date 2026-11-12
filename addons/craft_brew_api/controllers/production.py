from odoo import http
from odoo.http import request
from odoo.exceptions import ValidationError

from ..services.production import ProductionService
from .base import ApiError, BrewApiController


class ProductionController(BrewApiController):
    def _production_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, ProductionService(request.env, user, company)

    @staticmethod
    def _production_paging():
        return (
            max(int(request.httprequest.args.get("page", 1)), 1),
            min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100),
        )

    def _production_page_response(self, data, total, page, size, request_id):
        return self._response(
            data,
            meta={
                "page": page,
                "page_size": size,
                "total": total,
                "has_more": page * size < total,
            },
            request_id=request_id,
        )

    @http.route("/api/v1/material-requisitions", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def material_requisitions(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._production_context(mutation)
            if not mutation:
                page, size = self._production_paging()
                data, total = service.requisitions(
                    page, size, request.httprequest.args.get("state")
                )
                return self._production_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_requisition(payload)
            self._audit("material_requisition", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/material-requisitions/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def material_requisition_action(self, record_id, action):
        request_id = self._request_id()
        try:
            allowed = {
                "recommend-lots",
                "request-overage",
                "issue",
                "acknowledge",
                "return",
                "complete",
                "settle",
                "undo-issue",
            }
            if action not in allowed:
                raise ApiError("RESOURCE_NOT_FOUND", "领料动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._production_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            if action == "recommend-lots":
                record, data = service.recommend_lots(record_id, payload)
            else:
                record, data = service.requisition_action(record_id, action, payload)
            self._audit("material_requisition", action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except ValidationError as exc:
            return self._error_response(ApiError("VALIDATION_ERROR", str(exc), 422), request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-batches", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def batches(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._production_context(mutation)
            if not mutation:
                page, size = self._production_paging()
                data, total = service.batches(
                    page, size, request.httprequest.args.get("state")
                )
                return self._production_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            batch, data = service.create_batch(payload)
            self._audit("production_batch", "create", request_id, user, company, {"id": batch.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-batches/<int:batch_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def batch(self, batch_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._production_context()
            return self._response(service.batch_detail(batch_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-batches/<int:batch_id>/timeline", type="http", auth="public", methods=["GET"], csrf=False)
    def batch_timeline(self, batch_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._production_context()
            return self._response(service.batch_timeline(batch_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-batches/<int:batch_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def batch_action(self, batch_id, action):
        request_id = self._request_id()
        try:
            if action not in {"start", "enter-tank"}:
                raise ApiError("RESOURCE_NOT_FOUND", "生产批次动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._production_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            batch, data = service.batch_action(batch_id, action, payload)
            self._audit("production_batch", action, request_id, user, company, {"id": batch.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-records", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def records(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._production_context(mutation)
            if not mutation:
                page, size = self._production_paging()
                filters = {
                    key: request.httprequest.args.get(key)
                    for key in ["batch_id", "state", "record_type"]
                    if request.httprequest.args.get(key)
                }
                data, total = service.records(page, size, filters)
                return self._production_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_record(payload)
            self._audit("production_record", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-records/<int:record_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def record(self, record_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._production_context(mutation)
            if not mutation:
                record = service._record("brew.production.record", record_id)
                return self._response(service._serialize_record(record), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.update_record(record_id, payload)
            self._audit("production_record", "update", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-records/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def record_action(self, record_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "approve", "return", "correct"}:
                raise ApiError("RESOURCE_NOT_FOUND", "生产记录动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._production_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.record_action(record_id, action, payload)
            self._audit("production_record", action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201 if action == "correct" else 200)
        except Exception as exc:
            return self._error_response(exc, request_id)
