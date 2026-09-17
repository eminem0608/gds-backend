from odoo import http
from odoo.http import request

from ..services.fermentation import FermentationService
from .base import ApiError, BrewApiController


class FermentationController(BrewApiController):
    def _fermentation_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, FermentationService(request.env, user, company)

    @staticmethod
    def _fermentation_paging():
        return (
            max(int(request.httprequest.args.get("page", 1)), 1),
            min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100),
        )

    def _fermentation_page_response(self, data, total, page, size, request_id):
        return self._response(
            data,
            meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total},
            request_id=request_id,
        )

    def _fermentation_command(self, event_type, action, handler):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._fermentation_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = handler(service, payload)
            self._audit(event_type, action, request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/tanks", type="http", auth="public", methods=["GET"], csrf=False)
    def tanks(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._fermentation_context()
            page, size = self._fermentation_paging()
            filters = {
                key: request.httprequest.args.get(key)
                for key in ["factory_id", "status"]
                if request.httprequest.args.get(key)
            }
            data, total = service.tanks(page, size, filters)
            return self._fermentation_page_response(data, total, page, size, request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/tanks/<int:tank_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def tank(self, tank_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._fermentation_context()
            return self._response(service.tank_detail(tank_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/tanks/<int:tank_id>/timeline", type="http", auth="public", methods=["GET"], csrf=False)
    def tank_timeline(self, tank_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._fermentation_context()
            return self._response(service.tank_timeline(tank_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/add-yeast", type="http", auth="public", methods=["POST"], csrf=False)
    def add_yeast(self, session_id):
        return self._fermentation_command(
            "fermentation_session",
            "add_yeast",
            lambda service, payload: (
                lambda result: (result[0], {**result[1], "action_id": result[2]})
            )(service.add_yeast(session_id, payload)),
        )

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/actions", type="http", auth="public", methods=["POST"], csrf=False)
    def session_action(self, session_id):
        return self._fermentation_command(
            "fermentation_session",
            "action",
            lambda service, payload: (
                lambda result: (result[0], {**result[1], "action_id": result[2]})
            )(service.perform_action(session_id, payload)),
        )

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/transfer", type="http", auth="public", methods=["POST"], csrf=False)
    def transfer(self, session_id):
        return self._fermentation_command(
            "fermentation_session",
            "transfer",
            lambda service, payload: service.transfer(session_id, payload),
        )

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/complete", type="http", auth="public", methods=["POST"], csrf=False)
    def complete(self, session_id):
        return self._fermentation_command(
            "fermentation_session",
            "complete",
            lambda service, payload: service.complete_session(session_id, payload),
        )

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/readings", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def readings(self, session_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._fermentation_context(mutation)
            if not mutation:
                page, size = self._fermentation_paging()
                data, total = service.readings(session_id, page, size)
                return self._fermentation_page_response(data, total, page, size, request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create_reading(session_id, payload)
            self._audit("fermentation_reading", "create", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/fermentation-readings/<int:reading_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def reading_action(self, reading_id, action):
        if action not in {"submit", "approve", "correct", "return"}:
            request_id = self._request_id()
            return self._error_response(
                ApiError("RESOURCE_NOT_FOUND", "发酵读数动作不存在。", 404), request_id
            )
        return self._fermentation_command(
            "fermentation_reading",
            action,
            lambda service, payload: service.reading_action(reading_id, action, payload),
        )

    @http.route("/api/v1/fermentation-sessions/<int:session_id>/trends", type="http", auth="public", methods=["GET"], csrf=False)
    def trends(self, session_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._fermentation_context()
            return self._response(service.trends(session_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-anomalies", type="http", auth="public", methods=["GET"], csrf=False)
    def anomalies(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._fermentation_context()
            page, size = self._fermentation_paging()
            filters = {
                key: request.httprequest.args.get(key)
                for key in ["state", "severity"]
                if request.httprequest.args.get(key)
            }
            data, total = service.anomalies(page, size, filters)
            return self._fermentation_page_response(data, total, page, size, request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/production-anomalies/<int:anomaly_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def anomaly_action(self, anomaly_id, action):
        if action not in {"assign", "resolve", "verify"}:
            request_id = self._request_id()
            return self._error_response(
                ApiError("RESOURCE_NOT_FOUND", "异常动作不存在。", 404), request_id
            )
        return self._fermentation_command(
            "production_anomaly",
            action,
            lambda service, payload: service.anomaly_action(anomaly_id, action, payload),
        )
