from odoo import http
from odoo.http import request

from ..services.configuration import ConfigurationRuleService, RULE_PATHS
from .base import ApiError, BrewApiController


COLLECTION_ROUTES = [f"/api/v1/admin/{resource}" for resource in RULE_PATHS]
ITEM_ROUTES = [f"/api/v1/admin/{resource}/<int:record_id>" for resource in RULE_PATHS]
ACTION_ROUTES = [
    f"/api/v1/admin/{resource}/<int:record_id>/<string:action>" for resource in RULE_PATHS
]


class ConfigurationController(BrewApiController):
    def _configuration_resource(self):
        parts = request.httprequest.path.strip("/").split("/")
        resource = parts[3] if len(parts) > 3 else ""
        if resource not in RULE_PATHS:
            raise ApiError("RESOURCE_NOT_FOUND", "规则资源不存在。", 404)
        return resource

    def _configuration_context(self, mutation=False):
        user, company, _session, auth_mode = self._authenticate()
        if mutation:
            self._validate_cookie_csrf(auth_mode)
        return user, company, ConfigurationRuleService(request.env, user, company)

    @http.route(COLLECTION_ROUTES, type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def configuration_collection(self):
        request_id = self._request_id()
        try:
            resource = self._configuration_resource()
            mutation = request.httprequest.method == "POST"
            user, company, service = self._configuration_context(mutation)
            if not mutation:
                page = max(int(request.httprequest.args.get("page", 1)), 1)
                page_size = min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)
                data, total = service.list(
                    resource, page, page_size, request.httprequest.args.get("state")
                )
                return self._response(
                    data,
                    meta={"page": page, "page_size": page_size, "total": total, "has_more": page * page_size < total},
                    request_id=request_id,
                )
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            rule, data = service.create(resource, payload)
            self._audit("configuration", "create", request_id, user=user, company=company, summary={"resource": resource, "rule_id": rule.id})
            return self._complete_idempotency(idem, data, 201)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(ITEM_ROUTES, type="http", auth="public", methods=["GET", "PATCH"], csrf=False)
    def configuration_item(self, record_id):
        request_id = self._request_id()
        try:
            resource = self._configuration_resource()
            mutation = request.httprequest.method == "PATCH"
            user, company, service = self._configuration_context(mutation)
            if not mutation:
                return self._response(service.serialize(service._record(resource, record_id)), request_id=request_id)
            payload = self._json_body()
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            rule, data = service.update(resource, record_id, payload)
            self._audit("configuration", "update", request_id, user=user, company=company, summary={"resource": resource, "rule_id": rule.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route(ACTION_ROUTES, type="http", auth="public", methods=["POST"], csrf=False)
    def configuration_action(self, record_id, action):
        request_id = self._request_id()
        try:
            resource = self._configuration_resource()
            payload = self._json_body()
            user, company, service = self._configuration_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay:
                return replay
            rule, data = service.action(resource, record_id, action, payload.get("record_version"))
            request.env["brew.outbox.event"].sudo().enqueue(
                f"configuration_rule.{action}",
                "brew.configuration.rule",
                rule.id,
                {"rule_id": rule.id, "kind": rule.rule_kind, "code": rule.code, "version": rule.version},
                company,
            )
            self._audit("configuration", action, request_id, user=user, company=company, summary={"resource": resource, "rule_id": rule.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc:
            return self._error_response(exc, request_id)
