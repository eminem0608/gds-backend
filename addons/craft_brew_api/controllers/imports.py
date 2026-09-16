from odoo import http
from odoo.http import request

from ..services.imports import ImportService
from .base import BrewApiController


class ImportController(BrewApiController):
    def _context(self, mutation=False):
        user, company, _session, auth_mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, ImportService(request.env, user, company)

    @http.route("/api/v1/imports/template", type="http", auth="public", methods=["GET"], csrf=False)
    def template(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            return self._response(
                service.template(request.httprequest.args.get("resource")), request_id=request_id
            )
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/imports", type="http", auth="public", methods=["POST"], csrf=False)
    def create(self):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            batch, data = service.create(payload)
            self._audit("import", "create", request_id, user=user, company=company, summary={"batch_id": batch.id, "resource": batch.resource})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/imports/<int:batch_id>/validate", type="http", auth="public", methods=["POST"], csrf=False)
    def validate_batch(self, batch_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            batch, data = service.validate(batch_id, payload.get("version"))
            self._audit("import", "validate", request_id, user=user, company=company, summary={"batch_id": batch.id, "error_rows": batch.error_rows})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/imports/<int:batch_id>/report", type="http", auth="public", methods=["GET"], csrf=False)
    def report(self, batch_id):
        request_id = self._request_id()
        try:
            _user, _company, service = self._context()
            return self._response(service.report(batch_id), request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/imports/<int:batch_id>/commit", type="http", auth="public", methods=["POST"], csrf=False)
    def commit(self, batch_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            batch, data = service.commit(batch_id, payload.get("version"))
            request.env["brew.outbox.event"].sudo().enqueue(
                "master_data.import_committed",
                "brew.import.batch",
                batch.id,
                {"batch_id": batch.id, "resource": batch.resource, "total_rows": batch.total_rows},
                company,
            )
            self._audit("import", "commit", request_id, user=user, company=company, summary={"batch_id": batch.id, "resource": batch.resource, "total_rows": batch.total_rows})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
