from odoo import http
from odoo.http import request

from ..services.inventory import InventoryService
from .base import ApiError, BrewApiController


class InventoryController(BrewApiController):
    def _context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, InventoryService(request.env, user, company)

    @staticmethod
    def _paging():
        page = max(int(request.httprequest.args.get("page", 1)), 1)
        size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
        return page, size

    def _list_response(self, service_method, request_id, **filters):
        page, size = self._paging()
        data, total = service_method(page, size, filters)
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

    @http.route("/api/v1/inventory/summary", type="http", auth="public", methods=["GET"], csrf=False)
    def summary(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            return self._response(
                service.summary(
                    request.httprequest.args.get("factory_id"),
                    request.httprequest.args.get("location_id"),
                ),
                request_id=request_id,
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/lots", type="http", auth="public", methods=["GET"], csrf=False)
    def lots(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            filters = {
                key: request.httprequest.args.get(key)
                for key in [
                    "factory_id",
                    "location_id",
                    "product_id",
                    "quality_status",
                    "frozen",
                ]
                if request.httprequest.args.get(key) is not None
            }
            return self._list_response(service.lots, request_id, **filters)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/lots/<int:lot_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def lot(self, lot_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            return self._response(service.lot_detail(lot_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/lots/<int:lot_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def lot_action(self, lot_id, action):
        request_id = self._request_id()
        try:
            if action not in {"freeze", "unfreeze"}:
                raise ApiError("RESOURCE_NOT_FOUND", "批次动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            data = service.set_lot_frozen(
                lot_id, action == "freeze", payload.get("reason")
            )
            self._audit(
                "inventory_lot",
                action,
                request_id,
                user=user,
                company=company,
                summary={"lot_id": lot_id},
            )
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/movements", type="http", auth="public", methods=["GET"], csrf=False)
    def movements(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            filters = {
                key: request.httprequest.args.get(key)
                for key in ["factory_id", "location_id", "product_id", "lot_id"]
                if request.httprequest.args.get(key)
            }
            return self._list_response(service.movements, request_id, **filters)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/reservations", type="http", auth="public", methods=["GET"], csrf=False)
    def reservations(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            filters = {
                key: request.httprequest.args.get(key)
                for key in ["state", "source_model", "source_id"]
                if request.httprequest.args.get(key)
            }
            return self._list_response(service.reservations, request_id, **filters)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/transfers", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def transfers(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._context(mutation)
            if not mutation:
                page, size = self._paging()
                data, total = service.transfers(
                    page, size, request.httprequest.args.get("state")
                )
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
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_transfer(payload)
            self._audit("inventory_transfer", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/transfers/<int:document_id>/confirm", type="http", auth="public", methods=["POST"], csrf=False)
    def confirm_transfer(self, document_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.confirm_transfer(document_id, payload.get("version"))
            self._audit("inventory_transfer", "confirm", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/counts", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def counts(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._context(mutation)
            if not mutation:
                page, size = self._paging()
                data, total = service.counts(
                    page, size, request.httprequest.args.get("state")
                )
                return self._response(
                    data,
                    meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total},
                    request_id=request_id,
                )
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_count(payload)
            self._audit("inventory_count", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/counts/<int:document_id>/lines", type="http", auth="public", methods=["PUT"], csrf=False)
    def count_lines(self, document_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.update_count_lines(document_id, payload)
            self._audit("inventory_count", "update_lines", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/counts/<int:document_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def count_action(self, document_id, action):
        request_id = self._request_id()
        try:
            if action not in {"start", "submit", "confirm"}:
                raise ApiError("RESOURCE_NOT_FOUND", "盘点动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.count_action(document_id, action, payload)
            self._audit("inventory_count", action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/scraps", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def scraps(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._context(mutation)
            if not mutation:
                page, size = self._paging()
                data, total = service.scraps(
                    page, size, request.httprequest.args.get("state")
                )
                return self._response(
                    data,
                    meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total},
                    request_id=request_id,
                )
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_scrap(payload)
            self._audit("inventory_scrap", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/inventory/scraps/<int:document_id>/confirm", type="http", auth="public", methods=["POST"], csrf=False)
    def confirm_scrap(self, document_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.confirm_scrap(document_id, payload.get("version"))
            self._audit("inventory_scrap", "confirm", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
