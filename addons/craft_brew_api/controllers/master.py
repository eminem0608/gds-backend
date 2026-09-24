from odoo import http
from odoo.http import request

from ..services.master_data import MasterDataService, RESOURCE_CONFIG
from .base import ApiError, BrewApiController


COLLECTION_ROUTES = [f"/api/v1/{resource}" for resource in RESOURCE_CONFIG]
ITEM_ROUTES = [f"/api/v1/{resource}/<int:record_id>" for resource in RESOURCE_CONFIG]
ACTION_ROUTES = [
    f"/api/v1/{resource}/<int:record_id>/<string:action>" for resource in RESOURCE_CONFIG
]


class MasterDataController(BrewApiController):
    def _master_resource(self):
        parts = request.httprequest.path.strip("/").split("/")
        resource = parts[2] if len(parts) > 2 else ""
        if resource not in RESOURCE_CONFIG:
            raise ApiError("RESOURCE_NOT_FOUND", "主数据资源不存在。", 404)
        return resource

    def _master_context(self, mutation=False):
        user, company, _session, auth_mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, MasterDataService(request.env, user, company)

    @http.route(COLLECTION_ROUTES, type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def master_collection(self):
        request_id = self._request_id()
        try:
            resource = self._master_resource()
            mutation = request.httprequest.method == "POST"
            user, company, service = self._master_context(mutation=mutation)
            if not mutation:
                page = max(int(request.httprequest.args.get("page", 1)), 1)
                page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
                keyword = request.httprequest.args.get("keyword", "").strip()
                active_arg = request.httprequest.args.get("active")
                active = True if active_arg is None else (None if active_arg.lower() == "all" else active_arg.lower() in {"1", "true", "yes"})
                data, total = service.list(resource, page, page_size, keyword, active)
                return self._response(
                    data,
                    meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total},
                    request_id=request_id,
                )
            payload = self._json_body()
            service.ensure_write_access(resource)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.create(resource, payload)
            self._audit(
                "master_data",
                "create",
                request_id,
                user=user,
                company=company,
                summary={"resource": resource, "record_id": record.id},
            )
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(ITEM_ROUTES, type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def master_item(self, record_id):
        request_id = self._request_id()
        try:
            resource = self._master_resource()
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._master_context(mutation=mutation)
            if not mutation:
                return self._response(service.get(resource, record_id), request_id=request_id)
            payload = self._json_body()
            service.ensure_write_access(resource)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.update(resource, record_id, payload)
            self._audit(
                "master_data",
                "update",
                request_id,
                user=user,
                company=company,
                summary={"resource": resource, "record_id": record.id},
            )
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(ACTION_ROUTES, type="http", auth="public", methods=["POST"], csrf=False)
    def archive_restore(self, record_id, action):
        request_id = self._request_id()
        try:
            resource = self._master_resource()
            if action not in {"archive", "restore"}:
                raise ApiError("RESOURCE_NOT_FOUND", "主数据动作不存在。", 404)
            payload = self._json_body()
            user, company, service = self._master_context(mutation=True)
            service.ensure_write_access(resource)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            record, data = service.set_active(
                resource, record_id, action == "restore", payload.get("version")
            )
            self._audit(
                "master_data",
                action,
                request_id,
                user=user,
                company=company,
                summary={"resource": resource, "record_id": record.id},
            )
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/equipment/<int:record_id>/unavailability",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
    )
    def equipment_unavailability(self, record_id):
        request_id = self._request_id()
        try:
            mutation = request.httprequest.method == "POST"
            user, company, service = self._master_context(mutation)
            if not mutation:
                return self._response(service.list_unavailability(record_id), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            period, data = service.create_unavailability(record_id, payload)
            self._audit("equipment", "add_unavailability", request_id, user=user, company=company, summary={"equipment_id": record_id, "period_id": period.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(
        "/api/v1/equipment-unavailability/<int:record_id>",
        type="http",
        auth="public",
        methods=["PATCH"],
        csrf=False,
    )
    def update_equipment_unavailability(self, record_id):
        request_id = self._request_id()
        try:
            payload = self._json_body()
            user, company, service = self._master_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            period, data = service.update_unavailability(record_id, payload)
            self._audit("equipment", "update_unavailability", request_id, user=user, company=company, summary={"period_id": period.id, "state": period.state})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
