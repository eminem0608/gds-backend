from odoo import http
from odoo.http import request

from ..services.purchase import PurchaseService
from .base import ApiError, BrewApiController


class PurchaseController(BrewApiController):
    def _purchase_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, PurchaseService(request.env, user, company)

    @staticmethod
    def _purchase_paging():
        return (
            max(int(request.httprequest.args.get("page", 1)), 1),
            min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100),
        )

    def _purchase_page_response(self, data, total, page, size, request_id):
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

    @http.route("/api/v1/purchase-requisitions", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def purchase_requisitions(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                page, size = self._purchase_paging()
                data, total = service.requisitions(
                    page,
                    size,
                    {
                        key: request.httprequest.args.get(key)
                        for key in (
                            "keyword",
                            "supplier_name",
                            "requester_name",
                            "factory_id",
                            "request_type",
                            "state",
                            "date_from",
                            "date_to",
                        )
                        if request.httprequest.args.get(key)
                    },
                )
                return self._purchase_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_requisition(payload)
            self._audit("purchase_requisition", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-requisitions/source-plans", type="http", auth="public", methods=["GET"], csrf=False)
    def purchase_requisition_source_plans(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._purchase_context(False)
            filters = {
                key: request.httprequest.args.get(key)
                for key in (
                    "category_group", "category_code", "material_id", "material_name",
                    "plan_no", "product_name", "date_from", "date_to"
                )
                if request.httprequest.args.get(key)
            }
            return self._response(service.requisition_source_plans(filters), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-requisitions/<int:record_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def purchase_requisition(self, record_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                record = service._record("brew.purchase.requisition", record_id)
                return self._response(
                    service._serialize_requisition(record), request_id=request_id
                )
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.update_requisition(record_id, payload)
            self._audit("purchase_requisition", "update", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-requisitions/aggregate", type="http", auth="public", methods=["POST"], csrf=False)
    def aggregate_requisitions(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            records, data = service.aggregate_requisitions(payload)
            self._audit("purchase_requisition", "aggregate", request_id, user, company, {"ids": records.ids})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-requisitions/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def purchase_requisition_action(self, record_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "approve", "return"}:
                raise ApiError("RESOURCE_NOT_FOUND", "采购需求动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.requisition_action(record_id, action, payload)
            self._audit("purchase_requisition", action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders/from-requisitions", type="http", auth="public", methods=["POST"], csrf=False)
    def order_from_requisitions(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            orders, data = service.create_order_from_requisitions(payload)
            self._audit("purchase_order", "create_from_requisitions", request_id, user, company, {"ids": orders.ids})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def orders(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                page, size = self._purchase_paging()
                data, total = service.orders(
                    page,
                    size,
                    request.httprequest.args.get("state"),
                    request.httprequest.args.get("supplier_name"),
                    request.httprequest.args.get("order_no"),
                    request.httprequest.args.get("receipt_state"),
                )
                return self._purchase_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            order, data = service.create_order(payload)
            self._audit("purchase_order", "create", request_id, user, company, {"id": order.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders/<int:order_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def order(self, order_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                order = service._record("purchase.order", order_id, "brew_factory_id")
                return self._response(service._serialize_order(order, include_edit_options=True), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            order, data = service.update_order(order_id, payload)
            self._audit("purchase_order", "update", request_id, user, company, {"id": order.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders/<int:order_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def order_action(self, order_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "approve", "place", "close"}:
                raise ApiError("RESOURCE_NOT_FOUND", "采购订单动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            order, data = service.order_action(order_id, action, payload)
            self._audit("purchase_order", action, request_id, user, company, {"id": order.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders/<int:order_id>/shipments", type="http", auth="public", methods=["POST"], csrf=False)
    def create_shipment(self, order_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            shipment, data = service.create_shipment(order_id, payload)
            self._audit("supplier_shipment", "create", request_id, user, company, {"id": shipment.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/receipts", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def receipts(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                page, size = self._purchase_paging()
                data, total = service.receipts(
                    page,
                    size,
                    {
                        key: request.httprequest.args.get(key)
                        for key in (
                            "keyword",
                            "supplier_name",
                            "factory_id",
                            "state",
                            "date_from",
                            "date_to",
                        )
                        if request.httprequest.args.get(key)
                    },
                )
                return self._purchase_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            receipt, data = service.create_receipt(payload)
            self._audit("purchase_receipt", "create", request_id, user, company, {"id": receipt.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/receipts/<int:receipt_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def receipt_detail(self, receipt_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._purchase_context(mutation)
            if mutation:
                payload = self._json_body()
                idem, replay = self._start_idempotency(user, company, payload)
                if replay:
                    return replay
                record, data = service.update_receipt(receipt_id, payload)
                self._audit("purchase_receipt", "update", request_id, user, company, {"id": record.id})
                return self._complete_idempotency(idem, data)
            record = service._record("brew.receipt", receipt_id)
            return self._response(
                service._serialize_receipt(record), request_id=request_id
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/receipts/<int:receipt_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def receipt_action(self, receipt_id, action):
        request_id = self._request_id()
        try:
            if action not in {"confirm-arrival", "put-away"}:
                raise ApiError("RESOURCE_NOT_FOUND", "收货动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            receipt, data = service.receipt_action(receipt_id, action, payload)
            self._audit("purchase_receipt", action, request_id, user, company, {"id": receipt.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/quality-inspections", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def inspections(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                page, size = self._purchase_paging()
                data, total = service.inspections(
                    page,
                    size,
                    {
                        key: request.httprequest.args.get(key)
                        for key in (
                            "keyword",
                            "supplier_name",
                            "factory_id",
                            "state",
                            "date_from",
                            "date_to",
                        )
                        if request.httprequest.args.get(key)
                    },
                )
                return self._purchase_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            inspection, data = service.inspection_for_receipt(payload)
            return self._complete_idempotency(idem, data, 200)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/quality-inspections/<int:inspection_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def inspection_detail(self, inspection_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._purchase_context(False)
            record = service._record("brew.quality.inspection", inspection_id)
            return self._response(
                service._serialize_inspection(record), request_id=request_id
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/quality-inspections/<int:inspection_id>/results", type="http", auth="public", methods=["PUT"], csrf=False)
    def inspection_results(self, inspection_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            inspection, data = service.update_inspection_results(inspection_id, payload)
            self._audit("quality_inspection", "update_results", request_id, user, company, {"id": inspection.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/quality-inspections/<int:inspection_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def inspection_action(self, inspection_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit"}:
                raise ApiError("RESOURCE_NOT_FOUND", "质检动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            inspection, data = service.inspection_action(inspection_id, action, payload)
            self._audit("quality_inspection", action, request_id, user, company, {"id": inspection.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-returns", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def purchase_returns(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                page, size = self._purchase_paging()
                data, total = service.returns(
                    page,
                    size,
                    {
                        key: request.httprequest.args.get(key)
                        for key in (
                            "keyword",
                            "supplier_name",
                            "factory_id",
                            "material_category",
                            "state",
                            "date_from",
                            "date_to",
                        )
                        if request.httprequest.args.get(key)
                    },
                )
                return self._purchase_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_return(payload)
            self._audit("purchase_return", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-returns/<int:record_id>", type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def purchase_return(self, record_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._purchase_context(mutation)
            if not mutation:
                record = service._record("brew.purchase.return", record_id)
                return self._response(
                    service._serialize_return(record), request_id=request_id
                )
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.update_return(record_id, payload)
            self._audit("purchase_return", "update", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-orders/<int:order_id>/returnable-lines", type="http", auth="public", methods=["GET"], csrf=False)
    def purchase_order_returnable_lines(self, order_id):
        request_id = self._request_id()
        try:
            user, company, service = self._purchase_context(False)
            data = service.get_returnable_lines(order_id)
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/purchase-returns/<int:record_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def purchase_return_action(self, record_id, action):
        request_id = self._request_id()
        try:
            if action not in {"submit", "approve", "return", "execute", "cancel"}:
                raise ApiError("RESOURCE_NOT_FOUND", "退货动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._purchase_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.return_action(record_id, action, payload)
            self._audit("purchase_return", action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
