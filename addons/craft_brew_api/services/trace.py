from odoo import Command

from ..controllers.base import ApiError
from .inventory import InventoryService


TRACE_READ_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_operator",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_sales",
    "craft_brew_core.group_brew_admin",
}


class TraceService:
    def __init__(self, env, user, company):
        self.env, self.user, self.company = env, user, company
        if not any(user.has_group(group) for group in TRACE_READ_GROUPS):
            raise ApiError("PERMISSION_DENIED", "The current account cannot view packaging and trace data.", 403)
        self.inventory = InventoryService(env, user, company, enforce_access=False)

    def _require_production(self):
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_operator", "craft_brew_core.group_brew_production_director", "craft_brew_core.group_brew_admin"
        }):
            raise ApiError("PERMISSION_DENIED", "Production permission is required.", 403)

    def _require_warehouse(self):
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_warehouse", "craft_brew_core.group_brew_admin"
        }):
            raise ApiError("PERMISSION_DENIED", "Warehouse permission is required.", 403)

    def _require_sales_or_warehouse(self):
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_sales", "craft_brew_core.group_brew_warehouse", "craft_brew_core.group_brew_admin"
        }):
            raise ApiError("PERMISSION_DENIED", "Sales or warehouse permission is required.", 403)

    @staticmethod
    def _version(record):
        return record.write_date.isoformat() if record.write_date else None

    def _check_version(self, record, expected):
        current = self._version(record)
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "The record has changed; refresh and retry.", 409, {"current_version": current})

    def _record(self, model, record_id):
        record = self.env[model].sudo().search([
            ("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id)
        ], limit=1)
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "The requested packaging or trace record does not exist.", 404)
        if record.factory_id.id not in self.inventory._allowed_factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "The record is outside the current factory scope.", 403)
        return record

    def _serialize_packaging(self, record):
        return {
            "id": str(record.id), "name": record.name, "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "session": {"id": str(record.session_id.id), "name": record.session_id.name},
            "batch": {"id": str(record.batch_id.id), "name": record.batch_id.name},
            "product": {"id": str(record.product_id.id), "name": record.product_id.display_name},
            "packaging": {"id": str(record.packaging_id.id), "name": record.packaging_id.name, "level": record.packaging_id.brew_barcode_level},
            "finished_location": {"id": str(record.finished_location_id.id), "name": record.finished_location_id.complete_name},
            "planned_volume_l": record.planned_volume_l, "output_volume_l": record.output_volume_l,
            "output_qty": record.output_qty, "loss_volume_l": record.loss_volume_l,
            "finished_lot": ({"id": str(record.finished_lot_id.id), "name": record.finished_lot_id.name} if record.finished_lot_id else None),
            "estimated_material_cost": record.estimated_material_cost,
            "estimated_total_cost": record.estimated_total_cost,
            "materials": [{
                "id": str(line.id), "product": {"id": str(line.product_id.id), "name": line.product_id.display_name},
                "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name},
                "source_location": {"id": str(line.source_location_id.id), "name": line.source_location_id.complete_name},
                "planned_qty": line.planned_qty, "actual_qty": line.actual_qty, "loss_qty": line.loss_qty,
                "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
            } for line in record.material_line_ids],
            "code_count": len(record.code_ids), "released_at": record.released_at,
            "version": self._version(record),
        }

    def packaging_orders(self, page, page_size, state=None):
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self.inventory._allowed_factory_ids())]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.packaging.order"].sudo()
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self._serialize_packaging(record) for record in records], total

    def create_packaging_order(self, payload):
        self._require_production()
        session = self._record("brew.fermentation.session", payload.get("session_id"))
        product = self.inventory._product(payload.get("product_id"))
        packaging = self.env["product.packaging"].sudo().browse(int(payload.get("packaging_id") or 0)).exists()
        if not packaging:
            raise ApiError("RESOURCE_NOT_FOUND", "Packaging specification does not exist.", 404)
        location = self.inventory._location(payload.get("finished_location_id"))
        lines = []
        for item in payload.get("materials") or []:
            material = self.inventory._product(item.get("product_id"))
            lot = self.inventory._lot(item.get("lot_id"))
            source = self.inventory._location(item.get("source_location_id"))
            uom = self.env["uom.uom"].sudo().browse(int(item.get("uom_id") or material.uom_id.id)).exists()
            lines.append(Command.create({
                "product_id": material.id, "lot_id": lot.id, "source_location_id": source.id,
                "planned_qty": float(item.get("planned_qty") or 0), "actual_qty": float(item.get("actual_qty") or 0),
                "loss_qty": float(item.get("loss_qty") or 0), "uom_id": uom.id,
            }))
        record = self.env["brew.packaging.order"].sudo().create({
            "session_id": session.id, "product_id": product.id, "packaging_id": packaging.id,
            "finished_location_id": location.id, "planned_volume_l": payload.get("planned_volume_l") or session.current_volume_l,
            "material_line_ids": lines, "note": payload.get("note"),
        })
        return record, self._serialize_packaging(record)

    def packaging_action(self, record_id, action, payload):
        record = self._record("brew.packaging.order", record_id)
        self._check_version(record, payload.get("version"))
        if action == "start":
            self._require_production(); record.action_start()
        elif action == "submit":
            self._require_production()
            updates = {int(item.get("line_id") or 0): item for item in payload.get("materials") or []}
            for line in record.material_line_ids:
                item = updates.get(line.id, {})
                line.write({"actual_qty": float(item.get("actual_qty", line.actual_qty)), "loss_qty": float(item.get("loss_qty", line.loss_qty))})
            record.action_submit(payload.get("output_qty"), payload.get("output_volume_l"), payload.get("loss_volume_l"))
        elif action == "release":
            self._require_warehouse(); record.action_release(self.user, payload.get("confirmed"))
        elif action == "cancel":
            self._require_production(); record.action_cancel(payload.get("reason"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "Packaging action does not exist.", 404)
        return record, self._serialize_packaging(record)

    def generate_codes(self, record_id, payload):
        self._require_production()
        order = self._record("brew.packaging.order", record_id)
        self._check_version(order, payload.get("version"))
        parent = self.env["brew.trace.code"].sudo().browse(int(payload.get("parent_id") or 0)).exists()
        codes = self.env["brew.trace.code"].sudo().create_for_order(order, payload.get("code_type"), payload.get("quantity"), parent)
        return order, {"order": self._serialize_packaging(order), "codes": [self._serialize_code(code) for code in codes]}

    def _serialize_code(self, code):
        return {
            "id": str(code.id), "code": code.code, "code_type": code.code_type, "state": code.state,
            "product": {"id": str(code.product_id.id), "name": code.product_id.display_name},
            "lot": {"id": str(code.lot_id.id), "name": code.lot_id.name},
            "packaging_order_id": str(code.packaging_order_id.id),
            "parent_id": str(code.parent_id.id) if code.parent_id else None,
            "outbound_order_id": str(code.outbound_order_id.id) if code.outbound_order_id else None,
        }

    def codes(self, page, page_size, filters):
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self.inventory._allowed_factory_ids())]
        for key in ["code_type", "state", "lot_id"]:
            if filters.get(key): domain.append((key, "=", filters[key]))
        if filters.get("q"): domain.append(("code", "ilike", filters["q"]))
        model = self.env["brew.trace.code"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self._serialize_code(record) for record in records], total

    def _serialize_outbound(self, record):
        return {
            "id": str(record.id), "name": record.name, "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "customer": {"id": str(record.customer_id.id), "name": record.customer_id.display_name},
            "source_location": {"id": str(record.source_location_id.id), "name": record.source_location_id.complete_name},
            "logistics_provider": record.logistics_provider, "logistics_no": record.logistics_no,
            "logistics_source": record.logistics_source,
            "logistics_adapter_code": record.logistics_adapter_code,
            "delivery_address": record.delivery_address, "shipped_at": record.shipped_at,
            "lines": [{
                "id": str(line.id), "product": {"id": str(line.product_id.id), "name": line.product_id.display_name},
                "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name}, "quantity": line.quantity,
                "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                "codes": [code.code for code in line.trace_code_ids],
            } for line in record.line_ids], "version": self._version(record),
        }

    def outbound_orders(self, page, page_size, state=None):
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self.inventory._allowed_factory_ids())]
        if state: domain.append(("state", "=", state))
        model = self.env["brew.outbound.order"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self._serialize_outbound(record) for record in records], total

    def create_outbound(self, payload):
        self._require_sales_or_warehouse()
        if payload.get("logistics_source", "manual") != "manual":
            raise ApiError("LOGISTICS_ADAPTER_NOT_ENABLED", "V1 supports manually maintained logistics only.", 422)
        factory = self.inventory._factory(payload.get("factory_id")); location = self.inventory._location(payload.get("source_location_id"))
        customer = self.env["res.partner"].sudo().search([("id", "=", int(payload.get("customer_id") or 0)), "|", ("company_id", "=", False), ("company_id", "=", self.company.id)], limit=1)
        if not customer: raise ApiError("RESOURCE_NOT_FOUND", "Customer does not exist.", 404)
        lines = []
        for item in payload.get("lines") or []:
            product = self.inventory._product(item.get("product_id")); lot = self.inventory._lot(item.get("lot_id"))
            uom = self.env["uom.uom"].sudo().browse(int(item.get("uom_id") or product.uom_id.id)).exists()
            codes = self.env["brew.trace.code"].sudo().search([("company_id", "=", self.company.id), ("code", "in", item.get("codes") or [])])
            if len(codes) != len(set(item.get("codes") or [])): raise ApiError("TRACE_CODE_INVALID", "One or more trace codes do not exist.", 422)
            lines.append(Command.create({"product_id": product.id, "lot_id": lot.id, "quantity": float(item.get("quantity") or 0), "uom_id": uom.id, "trace_code_ids": [Command.set(codes.ids)]}))
        record = self.env["brew.outbound.order"].sudo().create({
            "company_id": self.company.id, "factory_id": factory.id, "customer_id": customer.id,
            "source_location_id": location.id, "logistics_provider": payload.get("logistics_provider"),
            "logistics_no": payload.get("logistics_no"), "delivery_address": payload.get("delivery_address"),
            "logistics_source": "manual", "note": payload.get("note"), "line_ids": lines,
        })
        return record, self._serialize_outbound(record)

    def outbound_action(self, record_id, action, payload):
        record = self._record("brew.outbound.order", record_id); self._check_version(record, payload.get("version"))
        if action == "ship": self._require_warehouse(); record.action_ship(self.user, payload.get("confirmed"))
        elif action == "cancel": self._require_sales_or_warehouse(); record.action_cancel(payload.get("reason"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "Outbound action does not exist.", 404)
        return record, self._serialize_outbound(record)

    def trace_code(self, value):
        code = self.env["brew.trace.code"].sudo().search([("company_id", "=", self.company.id), ("code", "=", value)], limit=1)
        if not code or code.factory_id.id not in self.inventory._allowed_factory_ids():
            raise ApiError("RESOURCE_NOT_FOUND", "Trace code does not exist.", 404)
        order, batch = code.packaging_order_id, code.packaging_order_id.batch_id
        return {
            "code": self._serialize_code(code),
            "packaging": self._serialize_packaging(order),
            "production": {"batch_id": str(batch.id), "batch_no": batch.name, "plan_id": str(batch.production_plan_id.id), "plan_no": batch.production_plan_id.name, "recipe_version_id": str(batch.recipe_version_id.id)},
            "materials": [self._material_lineage(item) for item in batch.material_actual_ids],
            "outbound": self._serialize_outbound(code.outbound_order_id) if code.outbound_order_id else None,
        }

    def _material_lineage(self, actual):
        lot = actual.lot_id
        receipt = self.env["brew.receipt"].sudo().browse(
            lot.brew_source_id if lot.brew_source_model == "brew.receipt" else 0
        ).exists()
        purchase = receipt.purchase_order_id if receipt else self.env["purchase.order"]
        inspection = self.env["brew.quality.inspection"].sudo().search(
            [("receipt_id", "=", receipt.id)], limit=1
        ) if receipt else self.env["brew.quality.inspection"]
        return {
            "product": actual.product_id.display_name,
            "lot_id": str(lot.id),
            "lot": lot.name,
            "supplier_lot": lot.brew_supplier_lot,
            "quantity": actual.quantity,
            "uom": actual.uom_id.name,
            "action_type": actual.action_type,
            "quality_status": lot.brew_quality_status,
            "supplier": ({"id": str(purchase.partner_id.id), "name": purchase.partner_id.display_name} if purchase else None),
            "purchase_order": ({"id": str(purchase.id), "name": purchase.name} if purchase else None),
            "receipt": ({"id": str(receipt.id), "name": receipt.name} if receipt else None),
            "inspection": ({"id": str(inspection.id), "name": inspection.name, "state": inspection.state} if inspection else None),
        }

    def forward_lot(self, lot_id):
        lot = self.inventory._lot(lot_id)
        actuals = self.env["brew.production.material.actual"].sudo().search([("company_id", "=", self.company.id), ("lot_id", "=", lot.id)])
        batches = actuals.mapped("batch_id").filtered(lambda item: item.factory_id.id in self.inventory._allowed_factory_ids())
        orders = self.env["brew.packaging.order"].sudo().search([("company_id", "=", self.company.id), ("batch_id", "in", batches.ids)])
        codes = orders.mapped("code_ids")
        return {
            "source_lot": {
                "id": str(lot.id), "name": lot.name, "product": lot.product_id.display_name,
                "supplier_lot": lot.brew_supplier_lot, "quality_status": lot.brew_quality_status,
                "source_model": lot.brew_source_model, "source_id": str(lot.brew_source_id) if lot.brew_source_id else None,
            },
            "production_batches": [{"id": str(batch.id), "name": batch.name} for batch in batches],
            "packaging_orders": [{"id": str(order.id), "name": order.name, "finished_lot": order.finished_lot_id.name} for order in orders],
            "trace_codes": [self._serialize_code(code) for code in codes],
            "outbound_orders": [{"id": str(order.id), "name": order.name, "customer": order.customer_id.display_name} for order in codes.mapped("outbound_order_id")],
        }
