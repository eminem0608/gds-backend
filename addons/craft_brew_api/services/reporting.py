import json
from datetime import timedelta

from odoo import fields

from ..controllers.base import ApiError
from .inventory import InventoryService


REPORT_GROUPS = {
    "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_warehouse", "craft_brew_core.group_brew_purchase",
    "craft_brew_core.group_brew_sales", "craft_brew_core.group_brew_finance",
    "craft_brew_core.group_brew_admin",
}


class ReportingService:
    def __init__(self, env, user, company, enforce_report_access=True):
        self.env, self.user, self.company = env, user, company
        if enforce_report_access and not any(user.has_group(group) for group in REPORT_GROUPS):
            raise ApiError("PERMISSION_DENIED", "The current account cannot view dashboards or reports.", 403)
        self.inventory = InventoryService(env, user, company, enforce_access=False)

    def _factory_ids(self, requested=None):
        allowed = self.inventory._allowed_factory_ids()
        if requested:
            value = int(requested)
            if value not in allowed:
                raise ApiError("DATA_SCOPE_DENIED", "Factory is outside the current account scope.", 403)
            return [value]
        return allowed

    def dashboard(self, factory_id=None):
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director",
            "craft_brew_core.group_brew_operator", "craft_brew_core.group_brew_warehouse",
            "craft_brew_core.group_brew_admin",
        }):
            raise ApiError("PERMISSION_DENIED", "The current account cannot view the factory dashboard.", 403)
        factory_ids = self._factory_ids(factory_id)
        task_domain = [("company_id", "=", self.company.id), ("state", "not in", ["done", "cancelled"]), "|", ("factory_id", "=", False), ("factory_id", "in", factory_ids)]
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director", "craft_brew_core.group_brew_admin"
        }):
            task_domain += ["|", ("assignee_id", "=", self.user.id), "&", ("assignee_id", "=", False), ("role_code", "in", self.user.brew_role_codes())]
        tanks = self.env["brew.tank"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("active", "=", True)])
        anomalies = self.env["brew.production.anomaly"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "!=", "closed")], order="severity desc,detected_at desc", limit=10)
        sessions = self.env["brew.fermentation.session"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "ready")])
        plans = self.env["brew.production.plan"].sudo()
        packaging = self.env["brew.packaging.order"].sudo()
        outbound = self.env["brew.outbound.order"].sudo()
        expiration_limit = fields.Date.today() + timedelta(days=30)
        locations = self.env["stock.location"].sudo().search([("brew_factory_id", "in", factory_ids), ("usage", "=", "internal")])
        scoped_lot_ids = self.env["stock.quant"].sudo().search([
            ("company_id", "=", self.company.id), ("location_id", "in", locations.ids),
            ("quantity", ">", 0), ("lot_id", "!=", False),
        ]).mapped("lot_id").ids
        lots = self.env["stock.lot"].sudo().search_count([
            ("id", "in", scoped_lot_ids), ("company_id", "=", self.company.id), ("brew_expiration_date", "!=", False),
            ("brew_expiration_date", "<=", expiration_limit), ("brew_quality_status", "!=", "rejected")
        ])
        return {
            "metrics": {
                "open_tasks": self.env["brew.task"].sudo().search_count(task_domain),
                "active_tanks": len(tanks.filtered(lambda item: item.status not in {"empty", "maintenance"})),
                "open_anomalies": len(anomalies),
                "ready_volume_l": sum(sessions.mapped("current_volume_l")),
                "plans_pending": plans.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "in", ["draft", "submitted", "approved"])]),
                "packaging_pending": packaging.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "in", ["draft", "in_progress", "pending_release"])]),
                "outbound_pending": outbound.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "draft")]),
                "expiring_lots": lots,
                "unread_notifications": self.env["brew.notification"].sudo().search_count([("company_id", "=", self.company.id), ("user_id", "=", self.user.id), ("read_at", "=", False)]),
            },
            "tanks": [{
                "id": str(tank.id), "name": tank.name, "factory": tank.factory_id.name,
                "status": tank.status, "cleaning_status": tank.cleaning_status,
                "product": tank.current_product, "batch": tank.current_batch_no,
                "volume_l": tank.current_volume_l, "capacity_l": tank.capacity_l,
                "temperature_c": tank.current_temperature, "pressure_bar": tank.current_pressure,
                "gravity_plato": tank.current_gravity,
            } for tank in tanks],
            "alerts": [{
                "id": str(item.id), "type": item.anomaly_type, "severity": item.severity,
                "state": item.state, "session": item.session_id.name, "tank": item.session_id.tank_id.name,
                "detected_at": item.detected_at,
            } for item in anomalies],
        }

    def report_summary(self, factory_id=None):
        factory_ids = self._factory_ids(factory_id)
        released = self.env["brew.packaging.order"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "released")])
        purchases = self.env["purchase.order"].sudo().search([("company_id", "=", self.company.id), ("brew_factory_id", "in", factory_ids), ("state", "in", ["purchase", "done"])])
        outbound = self.env["brew.outbound.order"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "shipped")])
        all_reports = any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"})
        production_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_production_director")
        purchase_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_purchase") or self.user.has_group("craft_brew_core.group_brew_finance")
        outbound_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_sales") or self.user.has_group("craft_brew_core.group_brew_warehouse")
        return {
            "production": {"released_batches": len(released) if production_allowed else 0, "output_volume_l": sum(released.mapped("output_volume_l")) if production_allowed else 0, "estimated_cost": sum(released.mapped("estimated_total_cost")) if production_allowed else 0},
            "purchase": {"orders": len(purchases) if purchase_allowed else 0, "amount_total": sum(purchases.mapped("amount_total")) if purchase_allowed else 0},
            "outbound": {"orders": len(outbound) if outbound_allowed else 0, "lines": len(outbound.mapped("line_ids")) if outbound_allowed else 0},
            "quality": {"open_anomalies": self.env["brew.production.anomaly"].sudo().search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "!=", "closed")]) if production_allowed else 0},
        }

    def _allowed_export_types(self):
        if any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_finance", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"}):
            return {"purchase", "inbound", "outbound", "inventory"}
        result = set()
        if self.user.has_group("craft_brew_core.group_brew_purchase"): result.add("purchase")
        if self.user.has_group("craft_brew_core.group_brew_warehouse"): result.update({"inbound", "outbound", "inventory"})
        if self.user.has_group("craft_brew_core.group_brew_sales"): result.add("outbound")
        return result

    def _serialize_export(self, record):
        return {
            "id": str(record.id), "name": record.name, "export_type": record.export_type,
            "state": record.state, "filters": json.loads(record.filters_json or "{}"),
            "factory_id": str(record.factory_id.id) if record.factory_id else None,
            "row_count": record.row_count, "error_message": record.error_message,
            "created_at": record.create_date, "completed_at": record.completed_at,
            "download_url": f"/api/v1/exports/{record.id}/download" if record.state == "done" else None,
        }

    def exports(self, page, page_size):
        domain = [("company_id", "=", self.company.id)]
        if not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"}):
            domain.append(("requested_by_id", "=", self.user.id))
        model = self.env["brew.export.job"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self._serialize_export(record) for record in records], total

    def create_export(self, payload):
        export_type = payload.get("export_type")
        if export_type not in self._allowed_export_types():
            raise ApiError("PERMISSION_DENIED", "This account cannot create the requested export type.", 403)
        factory = self.inventory._factory(payload.get("factory_id")) if payload.get("factory_id") else None
        filters = payload.get("filters") or {}
        if not isinstance(filters, dict):
            raise ApiError("VALIDATION_ERROR", "Export filters must be an object.", 422)
        record = self.env["brew.export.job"].sudo().create({
            "company_id": self.company.id, "factory_id": factory.id if factory else False,
            "requested_by_id": self.user.id, "export_type": export_type,
            "filters_json": json.dumps(filters, ensure_ascii=False, sort_keys=True),
        })
        return record, self._serialize_export(record)

    def export_record(self, record_id):
        record = self.env["brew.export.job"].sudo().search([("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id)], limit=1)
        if not record or (record.requested_by_id != self.user and not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"})):
            raise ApiError("RESOURCE_NOT_FOUND", "Export job does not exist.", 404)
        return record

    def notifications(self, page, page_size, unread_only=False):
        domain = [("company_id", "=", self.company.id), ("user_id", "=", self.user.id)]
        if unread_only: domain.append(("read_at", "=", False))
        model = self.env["brew.notification"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [{"id": str(item.id), "type": item.notification_type, "title": item.title, "body": item.body, "read_at": item.read_at, "created_at": item.create_date} for item in records], total

    def read_notification(self, record_id):
        record = self.env["brew.notification"].sudo().search([("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id), ("user_id", "=", self.user.id)], limit=1)
        if not record: raise ApiError("RESOURCE_NOT_FOUND", "Notification does not exist.", 404)
        record.action_read()
        return record, {"id": str(record.id), "read_at": record.read_at}

    def preference(self):
        record = self.env["brew.notification.preference"].sudo().search([("user_id", "=", self.user.id), ("company_id", "=", self.company.id)], limit=1)
        if not record:
            record = self.env["brew.notification.preference"].sudo().create({"user_id": self.user.id, "company_id": self.company.id})
        return record

    @staticmethod
    def serialize_preference(record):
        return {key: record[key] for key in ["in_app_enabled", "email_enabled", "wechat_enabled", "anomaly_enabled", "task_enabled", "export_enabled", "minimum_severity"]}

    def update_preference(self, payload):
        record = self.preference()
        allowed = {"in_app_enabled", "email_enabled", "wechat_enabled", "anomaly_enabled", "task_enabled", "export_enabled", "minimum_severity"}
        record.write({key: value for key, value in payload.items() if key in allowed})
        return record, self.serialize_preference(record)

    def operations_metrics(self):
        if not self.user.has_group("craft_brew_core.group_brew_admin"):
            raise ApiError("PERMISSION_DENIED", "Administrator permission is required.", 403)
        return {
            "outbox": {state: self.env["brew.outbox.event"].sudo().search_count([("company_id", "=", self.company.id), ("state", "=", state)]) for state in ["pending", "processing", "published", "failed"]},
            "exports": {state: self.env["brew.export.job"].sudo().search_count([("company_id", "=", self.company.id), ("state", "=", state)]) for state in ["queued", "running", "done", "failed"]},
            "active_sessions": self.env["brew.api.session"].sudo().search_count([("company_id", "=", self.company.id), ("revoked_at", "=", False)]),
        }
