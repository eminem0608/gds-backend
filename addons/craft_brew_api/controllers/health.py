from odoo import http
from odoo.http import request

from .base import BrewApiController


class HealthController(BrewApiController):
    @http.route("/api/v1/health/live", type="http", auth="none", methods=["GET"], csrf=False)
    def live(self):
        return request.make_json_response(
            {"data": {"status": "ok"}, "meta": {}, "error": None, "request_id": self._request_id()},
            headers=[("X-API-Version", "1")],
        )

    @http.route("/api/v1/health/ready", type="http", auth="public", methods=["GET"], csrf=False)
    def ready(self):
        request.env.cr.execute("SELECT 1")
        return self._response({"status": "ready", "database": True})
