import json
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


V1_CODE_TYPES = {"batch", "keg", "box"}


class BrewPackagingOrder(models.Model):
    _name = "brew.packaging.order"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Packaging Order"
    _order = "create_date desc,id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", related="session_id.company_id", store=True, index=True)
    factory_id = fields.Many2one("brew.factory", related="session_id.factory_id", store=True, index=True)
    session_id = fields.Many2one("brew.fermentation.session", required=True, check_company=True, index=True, ondelete="restrict")
    batch_id = fields.Many2one("brew.production.batch", related="session_id.batch_id", store=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True, index=True)
    packaging_id = fields.Many2one("product.packaging", required=True, check_company=True)
    finished_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    state = fields.Selection(
        [("draft", "Draft"), ("in_progress", "In progress"), ("pending_release", "Pending release"), ("released", "Released"), ("cancelled", "Cancelled")],
        required=True, default="draft", index=True,
    )
    planned_volume_l = fields.Float(required=True)
    output_volume_l = fields.Float()
    output_qty = fields.Float()
    loss_volume_l = fields.Float()
    started_at = fields.Datetime()
    submitted_at = fields.Datetime()
    released_at = fields.Datetime()
    released_by_id = fields.Many2one("res.users", check_company=True)
    finished_lot_id = fields.Many2one("stock.lot", check_company=True, readonly=True)
    output_move_id = fields.Many2one("stock.move", check_company=True, readonly=True)
    material_line_ids = fields.One2many("brew.packaging.material.line", "order_id")
    code_ids = fields.One2many("brew.trace.code", "packaging_order_id")
    estimated_material_cost = fields.Float(readonly=True)
    estimated_total_cost = fields.Float(readonly=True)
    note = fields.Text()

    _sql_constraints = [
        ("session_unique", "unique(session_id)", "A fermentation session can have only one packaging order."),
        ("planned_volume_positive", "check(planned_volume_l > 0)", "Planned volume must be positive."),
    ]

    @api.model_create_multi
    def create(self, values_list):
        records = self.browse()
        for values in values_list:
            session = self.env["brew.fermentation.session"].browse(values.get("session_id")).exists()
            if not session or session.state != "ready":
                raise ValidationError("Only a ready fermentation session can be packaged.")
            product = self.env["product.product"].browse(values.get("product_id")).exists()
            if not product or product.product_tmpl_id != session.batch_id.product_tmpl_id:
                raise ValidationError("The packaging product must match the production batch product.")
            packaging = self.env["product.packaging"].browse(values.get("packaging_id")).exists()
            if not packaging or packaging.product_id != product:
                raise ValidationError("The packaging specification must belong to the output product.")
            location = self.env["stock.location"].browse(values.get("finished_location_id")).exists()
            if not location or location.usage != "internal" or location.brew_factory_id != session.factory_id or location.brew_location_type != "finished":
                raise ValidationError("The output location must be a finished-goods location in the same factory.")
            values.setdefault("planned_volume_l", session.current_volume_l)
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.packaging.order") or "New"
            records |= super(BrewPackagingOrder, self).create(values)
        return records

    def action_start(self):
        for order in self:
            if order.state != "draft" or order.session_id.state != "ready":
                raise ValidationError("Only a draft order with a ready session can start.")
            order.write({"state": "in_progress", "started_at": fields.Datetime.now()})
        return True

    def action_submit(self, output_qty, output_volume_l, loss_volume_l=0):
        self.ensure_one()
        if self.state != "in_progress":
            raise ValidationError("Only an in-progress packaging order can be submitted.")
        quantity, volume, loss = float(output_qty or 0), float(output_volume_l or 0), float(loss_volume_l or 0)
        if quantity <= 0 or volume <= 0 or loss < 0:
            raise ValidationError("Output quantity and volume must be positive and loss cannot be negative.")
        if volume + loss > self.planned_volume_l + 0.001:
            raise ValidationError("Packaged volume plus loss cannot exceed the available tank volume.")
        for line in self.material_line_ids:
            if line.actual_qty < 0 or line.loss_qty < 0 or line.actual_qty + line.loss_qty > line.planned_qty + line.uom_id.rounding:
                raise ValidationError("Packaging material usage must be non-negative and cannot exceed its documented quantity.")
        self.write({"output_qty": quantity, "output_volume_l": volume, "loss_volume_l": loss, "state": "pending_release", "submitted_at": fields.Datetime.now()})
        return True

    def _estimated_process_cost(self):
        self.ensure_one()
        return sum(
            item.uom_id._compute_quantity(item.quantity, item.product_id.uom_id) * item.product_id.standard_price
            for item in self.batch_id.material_actual_ids
        )

    def action_release(self, user, confirmed):
        self.ensure_one()
        if confirmed is not True:
            raise ValidationError("Packaging release requires explicit confirmation.")
        if self.state != "pending_release":
            raise ValidationError("Only a pending packaging order can be released.")
        if self.product_id.tracking == "none":
            raise ValidationError("The finished product must use lot tracking before release.")
        production_location = self.env.ref("stock.stock_location_production")
        inventory_location = self.env.ref("stock.stock_location_inventory")
        material_cost = 0.0
        for line in self.material_line_ids:
            if line.actual_qty:
                line.consumption_move_id = self._brew_create_done_move(
                    name=f"{self.name} material consumption", product=line.product_id, quantity=line.actual_qty,
                    uom=line.uom_id, source_location=line.source_location_id, destination_location=production_location,
                    lot=line.lot_id, source_model=self._name, source_id=self.id, source_reference=self.name,
                )
            if line.loss_qty:
                line.loss_move_id = self._brew_create_done_move(
                    name=f"{self.name} packaging loss", product=line.product_id, quantity=line.loss_qty,
                    uom=line.uom_id, source_location=line.source_location_id, destination_location=inventory_location,
                    lot=line.lot_id, source_model=self._name, source_id=self.id, source_reference=self.name,
                )
            material_cost += line.uom_id._compute_quantity(line.actual_qty + line.loss_qty, line.product_id.uom_id) * line.product_id.standard_price
        shelf_life = self.product_id.product_tmpl_id.brew_shelf_life_days
        lot = self.env["stock.lot"].sudo().create({
            "name": self.env["ir.sequence"].next_by_code("brew.finished.lot") or self.name,
            "product_id": self.product_id.id, "company_id": self.company_id.id,
            "brew_expiration_date": fields.Date.today() + timedelta(days=shelf_life) if shelf_life else False,
            "brew_quality_status": "qualified", "brew_source_model": self._name, "brew_source_id": self.id,
        })
        output_move = self._brew_create_done_move(
            name=f"{self.name} finished goods receipt", product=self.product_id, quantity=self.output_qty,
            uom=self.product_id.uom_id, source_location=production_location, destination_location=self.finished_location_id,
            lot=lot, source_model=self._name, source_id=self.id, source_reference=self.name,
        )
        self.write({
            "state": "released", "released_at": fields.Datetime.now(), "released_by_id": user.id,
            "finished_lot_id": lot.id, "output_move_id": output_move.id,
            "estimated_material_cost": material_cost, "estimated_total_cost": material_cost + self._estimated_process_cost(),
        })
        self.session_id._complete_node("packaging_release", user)
        self.session_id.action_complete(user, True)
        self.env["brew.trace.code"].create_for_order(self, "batch", 1)
        return True

    def action_cancel(self, reason):
        for order in self:
            if order.state == "released" or not str(reason or "").strip():
                raise ValidationError("A released order cannot be cancelled and a reason is required.")
            order.write({"state": "cancelled", "note": reason})
        return True


class BrewPackagingMaterialLine(models.Model):
    _name = "brew.packaging.material.line"
    _description = "Craft Brew Packaging Material Line"
    _check_company_auto = True

    order_id = fields.Many2one("brew.packaging.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="order_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    source_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    planned_qty = fields.Float(required=True)
    actual_qty = fields.Float(default=0)
    loss_qty = fields.Float(default=0)
    uom_id = fields.Many2one("uom.uom", required=True)
    consumption_move_id = fields.Many2one("stock.move", check_company=True, readonly=True)
    loss_move_id = fields.Many2one("stock.move", check_company=True, readonly=True)

    @api.constrains("product_id", "lot_id", "uom_id", "source_location_id", "order_id")
    def _check_relations(self):
        for line in self:
            if line.product_id.product_tmpl_id.brew_item_type != "packaging_material":
                raise ValidationError("Only packaging-material products are allowed.")
            if line.lot_id.product_id != line.product_id:
                raise ValidationError("The material lot does not match the product.")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("The material unit category is invalid.")
            if line.source_location_id.brew_factory_id != line.order_id.factory_id:
                raise ValidationError("The material location must be in the order factory.")


class BrewTraceCode(models.Model):
    _name = "brew.trace.code"
    _description = "Craft Brew Trace Code"
    _order = "create_date,id"
    _check_company_auto = True

    code = fields.Char(required=True, copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    code_type = fields.Selection(
        [("batch", "Batch"), ("keg", "Keg"), ("box", "Box"), ("bottle", "Bottle"), ("can", "Can")],
        required=True, index=True,
    )
    parent_id = fields.Many2one("brew.trace.code", check_company=True, index=True, ondelete="restrict")
    child_ids = fields.One2many("brew.trace.code", "parent_id")
    packaging_order_id = fields.Many2one("brew.packaging.order", required=True, check_company=True, index=True, ondelete="restrict")
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True, index=True)
    state = fields.Selection(
        [("available", "Available"), ("packed", "Packed"), ("shipped", "Shipped"), ("void", "Void")],
        required=True, default="available", index=True,
    )
    outbound_order_id = fields.Many2one("brew.outbound.order", check_company=True, index=True)

    _sql_constraints = [
        ("company_code_unique", "unique(company_id,code)", "Trace codes must be unique within a company."),
    ]

    @api.model
    def create_for_order(self, order, code_type, quantity, parent=None):
        if code_type not in V1_CODE_TYPES:
            raise ValidationError("V1 supports batch, keg and box codes only.")
        if order.state != "released" or not order.finished_lot_id:
            raise ValidationError("Trace codes can be generated only after packaging release.")
        count = int(quantity or 0)
        if count <= 0 or count > 10000:
            raise ValidationError("Trace code quantity must be between 1 and 10000.")
        if code_type in {"keg", "box"} and order.packaging_id.brew_barcode_level != code_type:
            raise ValidationError("The requested code level does not match the packaging specification.")
        if parent and parent.packaging_order_id != order:
            raise ValidationError("Parent and child codes must belong to the same packaging order.")
        result = self.browse()
        rule = self.env["brew.configuration.rule"].sudo().search(
            [
                ("company_id", "=", order.company_id.id),
                ("rule_kind", "=", "barcode"),
                ("code", "=", code_type),
                ("state", "=", "active"),
                "|",
                ("factory_id", "=", order.factory_id.id),
                ("factory_id", "=", False),
            ],
            order="factory_id desc,version desc",
            limit=1,
        )
        config = json.loads(rule.config_json or "{}") if rule else {}
        prefix = str(config.get("prefix") or {"batch": "BAT", "keg": "KEG", "box": "BOX"}[code_type]).strip().upper()
        if not prefix:
            raise ValidationError("The active barcode rule prefix cannot be empty.")
        include_factory = config.get("include_factory", True) is not False
        for _index in range(count):
            sequence = self.env["ir.sequence"].next_by_code("brew.trace.code") or "0"
            parts = [prefix]
            if include_factory:
                parts.append(order.factory_id.code)
            parts.append(sequence)
            result |= self.create({
                "code": "-".join(parts),
                "company_id": order.company_id.id, "factory_id": order.factory_id.id,
                "code_type": code_type, "parent_id": parent.id if parent else False,
                "packaging_order_id": order.id, "lot_id": order.finished_lot_id.id,
                "product_id": order.product_id.id, "state": "packed" if parent else "available",
            })
        return result


class BrewOutboundOrder(models.Model):
    _name = "brew.outbound.order"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Outbound Order"
    _order = "create_date desc,id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    customer_id = fields.Many2one("res.partner", required=True, check_company=True, index=True)
    source_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    state = fields.Selection(
        [("draft", "Draft"), ("shipped", "Shipped"), ("cancelled", "Cancelled")],
        required=True, default="draft", index=True,
    )
    requested_at = fields.Datetime(default=fields.Datetime.now)
    shipped_at = fields.Datetime()
    shipped_by_id = fields.Many2one("res.users", check_company=True)
    logistics_provider = fields.Char()
    logistics_no = fields.Char(index=True)
    logistics_source = fields.Selection(
        [("manual", "Manual"), ("adapter", "Platform adapter")],
        required=True,
        default="manual",
        index=True,
    )
    logistics_adapter_code = fields.Char(index=True)
    delivery_address = fields.Char()
    note = fields.Text()
    line_ids = fields.One2many("brew.outbound.order.line", "order_id")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.outbound.order") or "New"
        return super().create(values_list)

    @api.constrains("source_location_id", "factory_id")
    def _check_location(self):
        for order in self:
            if order.source_location_id.usage != "internal" or order.source_location_id.brew_factory_id != order.factory_id:
                raise ValidationError("The outbound source must be an internal location in the selected factory.")

    def action_ship(self, user, confirmed):
        self.ensure_one()
        if confirmed is not True:
            raise ValidationError("Outbound shipment requires explicit confirmation.")
        if self.state != "draft" or not self.line_ids:
            raise ValidationError("Only a draft outbound order with lines can be shipped.")
        destination = self.customer_id.property_stock_customer
        for line in self.line_ids:
            codes = line.trace_code_ids
            if not codes:
                raise ValidationError("Each outbound line must include at least one scanned trace code.")
            if any(code.state not in {"available", "packed"} for code in codes):
                raise ValidationError("One or more trace codes are unavailable for shipment.")
            if any(code.lot_id != line.lot_id or code.product_id != line.product_id for code in codes):
                raise ValidationError("Trace code product or lot does not match the outbound line.")
            line.move_id = self._brew_create_done_move(
                name=f"{self.name} shipment", product=line.product_id, quantity=line.quantity,
                uom=line.uom_id, source_location=self.source_location_id, destination_location=destination,
                lot=line.lot_id, source_model=self._name, source_id=self.id, source_reference=self.name,
            )
            codes.write({"state": "shipped", "outbound_order_id": self.id})
        self.write({"state": "shipped", "shipped_at": fields.Datetime.now(), "shipped_by_id": user.id})
        return True

    def action_cancel(self, reason):
        for order in self:
            if order.state != "draft" or not str(reason or "").strip():
                raise ValidationError("Only a draft order can be cancelled and a reason is required.")
            order.write({"state": "cancelled", "note": reason})
        return True


class BrewOutboundOrderLine(models.Model):
    _name = "brew.outbound.order.line"
    _description = "Craft Brew Outbound Order Line"
    _check_company_auto = True

    order_id = fields.Many2one("brew.outbound.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="order_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    trace_code_ids = fields.Many2many("brew.trace.code", "brew_outbound_line_trace_rel", "line_id", "trace_code_id")
    move_id = fields.Many2one("stock.move", check_company=True, readonly=True)

    _sql_constraints = [("quantity_positive", "check(quantity > 0)", "Outbound quantity must be positive.")]

    @api.constrains("product_id", "lot_id", "uom_id")
    def _check_relations(self):
        for line in self:
            if line.lot_id.product_id != line.product_id:
                raise ValidationError("The outbound lot does not match the product.")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("The outbound unit category is invalid.")
