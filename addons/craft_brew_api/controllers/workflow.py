from odoo import http
from odoo.http import request

from ..services.workflow import WorkflowService
from .base import ApiError, BrewApiController


class WorkflowController(BrewApiController):
    def _workflow_context(self, mutation=False):
        user, company, _session, mode = self._authenticate()
        if mutation: self._validate_cookie_csrf(mode)
        return user, company, WorkflowService(request.env, user, company)

    def _pagination(self):
        return max(int(request.httprequest.args.get("page", 1)), 1), min(max(int(request.httprequest.args.get("page_size", 20)), 1), 100)

    @http.route("/api/v1/tasks", type="http", auth="public", methods=["GET"], csrf=False)
    def tasks(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._workflow_context(); page, size = self._pagination()
            data, total = service.list_tasks(page, size, request.httprequest.args.get("state"), task_type=request.httprequest.args.get("task_type"), active_only=request.httprequest.args.get("active_only") == "true")
            return self._response(data, meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total}, request_id=request_id)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/my-workbench", type="http", auth="public", methods=["GET"], csrf=False)
    def my_workbench(self):
        request_id = self._request_id()
        try:
            user, company, _service = self._workflow_context()
            from ..services.workbench import WorkbenchService
            data = WorkbenchService(request.env, user, company).get(
                request.httprequest.args.get("date"), request.httprequest.args.get("month"),
                include_history=request.httprequest.args.get("summary") != "true")
            return self._response(data, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)

    @http.route("/api/v1/tasks/<int:task_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def task_action(self, task_id, action):
        request_id = self._request_id()
        try:
            if action not in {"claim", "start", "submit", "return", "reassign", "checkpoint", "fermentation-record", "fermentation-condition", "fermentation-test-next-day", "issue-materials", "prepare-materials", "acknowledge-materials", "packaging-start", "packaging-complete"}: raise ApiError("RESOURCE_NOT_FOUND", "任务动作不存在。", 404)
            payload = self._json_body(); user, company, service = self._workflow_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            task, data = service.task_action(task_id, action, payload)
            self._audit("task", action, request_id, user=user, company=company, summary={"task_id": task.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/approvals", type="http", auth="public", methods=["GET"], csrf=False)
    def approvals(self):
        request_id = self._request_id()
        try:
            _user, _company, service = self._workflow_context(); page, size = self._pagination()
            data, total = service.list_approvals(page, size, request.httprequest.args.get("state"))
            return self._response(data, meta={"page": page, "page_size": size, "total": total, "has_more": page * size < total}, request_id=request_id)
        except Exception as exc: return self._error_response(exc, request_id)

    @http.route("/api/v1/approvals/<int:step_id>/<string:action>", type="http", auth="public", methods=["POST"], csrf=False)
    def approval_action(self, step_id, action):
        request_id = self._request_id()
        try:
            if action not in {"approve", "return", "transfer"}: raise ApiError("RESOURCE_NOT_FOUND", "审批动作不存在。", 404)
            payload = self._json_body(); user, company, service = self._workflow_context(True)
            idem, replay = self._start_idempotency(user, company, payload)
            if replay: return replay
            step, data = service.approval_action(step_id, action, payload)
            self._audit("approval", action, request_id, user=user, company=company, summary={"step_id": step.id, "instance_id": step.instance_id.id})
            return self._complete_idempotency(idem, data)
        except Exception as exc: return self._error_response(exc, request_id)
