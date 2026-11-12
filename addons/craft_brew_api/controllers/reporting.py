import base64

from odoo import http
from odoo.http import request

from ..services.reporting import ReportingService
from .base import BrewApiController


class ReportingController(BrewApiController):
    def _reporting_context(self, mutation=False, enforce_report_access=True):
        user, company, _session, mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(mode)
        return user, company, ReportingService(
            request.env,
            user,
            company,
            enforce_report_access=enforce_report_access,
        )

    @staticmethod
    def _reporting_paging():
        return max(int(request.httprequest.args.get("page", 1)), 1), min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)

    def _reporting_page(self, data, total, page, size, request_id):
        return self._response(data, meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total}, request_id=request_id)

    @http.route("/api/v1/dashboard/overview", type="http", auth="public", methods=["GET"], csrf=False)
    def dashboard(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context(enforce_report_access=False)
            return self._response(service.dashboard(request.httprequest.args.get("factory_id")), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/reports/summary", type="http", auth="public", methods=["GET"], csrf=False)
    def report_summary(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context()
            return self._response(service.report_summary(request.httprequest.args.get("factory_id")), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/reports/catalog", type="http", auth="public", methods=["GET"], csrf=False)
    def report_catalog(self):
        request_id = self._request_id()
        try:
            from ..services.report_data import ReportDataService
            user, company, _session, _mode = self._authenticate()
            return self._response(ReportDataService(request.env, user, company).catalog(), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(["/api/v1/reports/data/<string:code>", "/api/v1/reports/data/<string:code>/download"], type="http", auth="public", methods=["GET"], csrf=False)
    def report_data(self, code):
        request_id = self._request_id()
        try:
            from urllib.parse import quote
            from ..services.report_data import ReportDataService
            user, company, _session, _mode = self._authenticate()
            service = ReportDataService(request.env, user, company)
            filters = {key: request.httprequest.args.get(key) for key in ['factory_id','keyword','date_from','date_to']}
            if request.httprequest.path.endswith('/download'):
                title, content = service.csv(code, filters)
                return request.make_response(content, headers=[('Content-Type','text/csv; charset=utf-8'),
                    ('Content-Disposition', "attachment; filename*=UTF-8''" + quote(title + '.csv')), ('Cache-Control','no-store')])
            page, size = self._reporting_paging()
            return self._response(service.data(code, filters, page, size), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/exports", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def exports(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"; user, company, service = self._reporting_context(mutation)
            if not mutation:
                page, size = self._reporting_paging(); data, total = service.exports(page, size)
                return self._reporting_page(data, total, page, size, request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = service.create_export(payload)
            self._audit("export_job", "create", request_id, user, company, {"id": record.id, "type": record.export_type})
            return self._complete_idempotency(idem, data, 202)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/exports/<int:record_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def export_detail(self, record_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context(); record = service.export_record(record_id)
            return self._response(service._serialize_export(record), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/exports/<int:record_id>/download", type="http", auth="public", methods=["GET"], csrf=False)
    def export_download(self, record_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context(); record = service.export_record(record_id)
            if record.state != "done" or not record.attachment_id:
                from .base import ApiError
                raise ApiError("EXPORT_NOT_READY", "Export file is not ready.", 409)
            content = base64.b64decode(record.attachment_id.datas or b"")
            return request.make_response(content, headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="{record.name}-{record.export_type}.csv"'),
                ("X-Request-ID", request_id),
            ])
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/notifications", type="http", auth="public", methods=["GET"], csrf=False)
    def notifications(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context(); page, size = self._reporting_paging()
            data, total = service.notifications(page, size, request.httprequest.args.get("unread_only") == "true")
            return self._reporting_page(data, total, page, size, request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/notifications/<int:record_id>/read", type="http", auth="public", methods=["POST"], csrf=False)
    def notification_read(self, record_id):
        request_id = self._request_id()
        try:
            payload = self._json_body(); user, company, service = self._reporting_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = service.read_notification(record_id)
            self._audit("notification", "read", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/notification-preferences", type="http", auth="public", methods=["GET", "PUT"], csrf=False)
    def notification_preferences(self):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "PUT"; user, company, service = self._reporting_context(mutation)
            if not mutation:
                return self._response(service.serialize_preference(service.preference()), request_id=request_id)
            payload = self._json_body(); idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            record, data = service.update_preference(payload)
            self._audit("notification_preference", "update", request_id, user, company, {"id": record.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/admin/operations/metrics", type="http", auth="public", methods=["GET"], csrf=False)
    def operations_metrics(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._reporting_context()
            return self._response(service.operations_metrics(), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)
