from odoo import http

from .base import BrewApiController


DICTIONARIES = {
    "warehouse_types": [
        {"value": "raw_material", "label": "原料仓"},
        {"value": "packaging", "label": "包装物仓"},
        {"value": "semi_finished", "label": "半成品库"},
        {"value": "finished", "label": "成品库"},
        {"value": "qc", "label": "待检库"},
        {"value": "loss", "label": "损耗库"},
    ],
    "equipment_statuses": [
        {"value": "available", "label": "可用"},
        {"value": "busy", "label": "使用中"},
        {"value": "cleaning", "label": "清洗中"},
        {"value": "maintenance", "label": "维护中"},
        {"value": "disabled", "label": "停用"},
    ],
    "material_categories": [
        {"value": "malt", "label": "麦芽"},
        {"value": "hop", "label": "酒花"},
        {"value": "yeast", "label": "酵母"},
        {"value": "adjunct", "label": "辅料"},
        {"value": "packaging", "label": "包装物"},
    ],
    "uoms": [
        {"value": "kg", "label": "kg"},
        {"value": "g", "label": "g"},
        {"value": "l", "label": "L"},
        {"value": "unit", "label": "个"},
        {"value": "box", "label": "箱"},
    ],
    "barcode_levels": [
        {"value": "batch", "label": "批次码"},
        {"value": "keg", "label": "桶码"},
        {"value": "box", "label": "箱码"},
    ],
}


class CommonController(BrewApiController):
    @http.route("/api/v1/dictionaries", type="http", auth="public", methods=["GET"], csrf=False)
    def dictionaries(self):
        request_id = self._request_id()
        try:
            self._authenticate()
            return self._response(DICTIONARIES, request_id=request_id)
        except Exception as exc:
            return self._error_response(exc, request_id)
