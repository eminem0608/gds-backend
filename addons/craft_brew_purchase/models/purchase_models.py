from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


class BrewPurchaseRequisition(models.Model):
    _name = "brew.purchase.requisition"
    _description = "Craft Brew Purchase Requisition"
    _order = "need_date, id"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    need_date = fields.Date(required=True, index=True)
    reason = fields.Text()
    requester_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, check_company=True
    )
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("submitted", "待审批"),
            ("approved", "已批准"),
            ("returned", "已退回"),
            ("ordered", "已转订单"),
            ("closed", "已关闭"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many("brew.purchase.requisition.line", "requisition_id")
    approval_instance_id = fields.Many2one(
        "brew.approval.instance", check_company=True, readonly=True
    )
    return_reason = fields.Text()

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.purchase.requisition") or "New"
                )
        return super().create(values_list)

    def action_submit(self, user):
        self.ensure_one()
        if self.state not in {"draft", "returned"} or not self.line_ids:
            raise ValidationError("只有包含明细的草稿或退回采购需求可以提交。")
        total = sum(
            line.uom_id._compute_quantity(line.requested_qty, line.product_id.uom_id)
            * line.product_id.standard_price
            for line in self.line_ids
        )
        approval = self.env["brew.approval.instance"].create_for_target(
            "purchase_requisition",
            self,
            user,
            self.factory_id,
            {"amount": total, "quantity": sum(self.line_ids.mapped("requested_qty"))},
        )
        self.write(
            {
                "state": "submitted",
                "approval_instance_id": approval.id,
                "return_reason": False,
            }
        )
        return approval

    def action_approve(self):
        self.ensure_one()
        if (
            self.state != "submitted"
            or not self.approval_instance_id
            or self.approval_instance_id.state != "approved"
        ):
            raise ValidationError("采购需求审批尚未通过。")
        for line in self.line_ids:
            if not line.approved_qty:
                line.approved_qty = line.requested_qty
        self.write({"state": "approved"})
        return True

    def action_return(self, reason):
        self.ensure_one()
        if self.state != "submitted" or not str(reason or "").strip():
            raise ValidationError("只有待审批采购需求可退回，且必须填写原因。")
        self.write({"state": "returned", "return_reason": reason})
        return True


class BrewPurchaseRequisitionLine(models.Model):
    _name = "brew.purchase.requisition.line"
    _description = "Craft Brew Purchase Requisition Line"
    _check_company_auto = True

    requisition_id = fields.Many2one(
        "brew.purchase.requisition", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="requisition_id.company_id", store=True, index=True
    )
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    requested_qty = fields.Float(required=True)
    approved_qty = fields.Float(default=0)
    ordered_qty = fields.Float(default=0, readonly=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    need_date = fields.Date(related="requisition_id.need_date", store=True)
    allocation_ids = fields.One2many(
        "brew.purchase.requisition.allocation", "requisition_line_id"
    )

    _sql_constraints = [
        ("requested_positive", "check(requested_qty > 0)", "采购需求数量必须大于零。"),
        ("approved_non_negative", "check(approved_qty >= 0)", "批准数量不能小于零。"),
        ("ordered_non_negative", "check(ordered_qty >= 0)", "已下单数量不能小于零。"),
    ]

    @api.constrains("product_id", "uom_id", "approved_qty", "requested_qty")
    def _check_values(self):
        for line in self:
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("采购需求单位与物料单位类别不一致。")
            if line.approved_qty > line.requested_qty:
                raise ValidationError("批准数量不能超过申请数量。")


class BrewPurchaseRequisitionAllocation(models.Model):
    _name = "brew.purchase.requisition.allocation"
    _description = "Craft Brew Purchase Requisition Plan Allocation"
    _check_company_auto = True

    requisition_line_id = fields.Many2one(
        "brew.purchase.requisition.line", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="requisition_line_id.company_id", store=True, index=True
    )
    production_plan_id = fields.Many2one(
        "brew.production.plan", required=True, check_company=True, index=True
    )
    required_qty = fields.Float(required=True)
    shortage_qty = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)

    _sql_constraints = [
        ("required_positive", "check(required_qty > 0)", "计划需求数量必须大于零。"),
        ("shortage_positive", "check(shortage_qty > 0)", "缺口数量必须大于零。"),
    ]


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    brew_factory_id = fields.Many2one(
        "brew.factory", string="厂区", check_company=True, index=True
    )
    brew_requisition_ids = fields.Many2many(
        "brew.purchase.requisition",
        "brew_purchase_order_requisition_rel",
        "order_id",
        "requisition_id",
        string="采购需求",
    )
    brew_approval_instance_id = fields.Many2one(
        "brew.approval.instance", check_company=True, readonly=True
    )
    brew_approval_state = fields.Selection(
        [
            ("none", "未提交"),
            ("pending", "待审批"),
            ("approved", "已批准"),
            ("returned", "已退回"),
        ],
        default="none",
        required=True,
        index=True,
    )
    brew_placed_at = fields.Datetime(readonly=True)
    brew_close_reason = fields.Text(readonly=True)
    brew_supplier_shipment_ids = fields.One2many(
        "brew.supplier.shipment", "purchase_order_id"
    )
    brew_receipt_ids = fields.One2many("brew.receipt", "purchase_order_id")

    def _create_picking(self):
        if self.env.context.get("brew_skip_standard_picking"):
            return True
        return super()._create_picking()

    def action_brew_submit(self, user):
        self.ensure_one()
        if self.state not in {"draft", "sent"} or self.brew_approval_state not in {
            "none",
            "returned",
        }:
            raise ValidationError("当前采购订单不能提交审批。")
        if not self.brew_factory_id or not self.order_line:
            raise ValidationError("采购订单必须填写厂区和明细。")
        approval = self.env["brew.approval.instance"].create_for_target(
            "purchase_order",
            self,
            user,
            self.brew_factory_id,
            {"amount": self.amount_total, "quantity": sum(self.order_line.mapped("product_qty"))},
        )
        self.write(
            {
                "brew_approval_state": "pending",
                "brew_approval_instance_id": approval.id,
            }
        )
        return approval

    def action_brew_approve(self):
        self.ensure_one()
        if (
            self.brew_approval_state != "pending"
            or self.brew_approval_instance_id.state != "approved"
        ):
            raise ValidationError("采购订单审批尚未通过。")
        self.write({"brew_approval_state": "approved"})
        return True

    def action_brew_place(self):
        self.ensure_one()
        if self.brew_approval_state != "approved" or self.state not in {"draft", "sent"}:
            raise ValidationError("只有已批准的询价单可以正式下单。")
        self.with_context(brew_skip_standard_picking=True).button_confirm()
        if self.state == "to approve":
            self.button_approve()
        self.write({"brew_placed_at": fields.Datetime.now()})
        self.brew_requisition_ids.write({"state": "ordered"})
        return True

    def action_brew_close(self, reason=None):
        self.ensure_one()
        if self.state != "purchase":
            raise ValidationError("只有已下单采购订单可以关闭。")
        ordered = sum(
            line.product_uom._compute_quantity(line.product_qty, line.product_id.uom_id)
            for line in self.order_line.filtered(lambda item: not item.display_type)
        )
        received = sum(
            line.product_id.uom_id._compute_quantity(line.received_qty, line.product_id.uom_id)
            for receipt in self.brew_receipt_ids.filtered(lambda item: item.state in {"arrived", "done"})
            for line in receipt.line_ids
        )
        if received + 1e-6 < ordered and not str(reason or "").strip():
            raise ValidationError("采购订单短交关闭必须填写原因。")
        self.button_done()
        self.write({"brew_close_reason": reason})
        return True


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    brew_requisition_line_ids = fields.Many2many(
        "brew.purchase.requisition.line",
        "brew_purchase_order_line_requisition_rel",
        "order_line_id",
        "requisition_line_id",
        string="采购需求明细",
    )


class BrewSupplierShipment(models.Model):
    _name = "brew.supplier.shipment"
    _description = "Craft Brew Supplier Shipment"
    _order = "shipped_at desc, id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="purchase_order_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="purchase_order_id.brew_factory_id", store=True, index=True
    )
    purchase_order_id = fields.Many2one(
        "purchase.order", required=True, check_company=True, index=True
    )
    carrier_name = fields.Char(required=True)
    tracking_number = fields.Char(required=True, index=True)
    shipped_at = fields.Datetime(required=True)
    expected_arrival = fields.Datetime()
    actual_arrival = fields.Datetime(readonly=True)
    state = fields.Selection(
        [
            ("shipped", "已发货"),
            ("in_transit", "运输中"),
            ("arrived", "已到货"),
            ("exception", "物流异常"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="shipped",
        index=True,
    )
    line_ids = fields.One2many("brew.supplier.shipment.line", "shipment_id")
    note = fields.Text()

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.supplier.shipment") or "New"
                )
        return super().create(values_list)

    @api.constrains("purchase_order_id", "line_ids")
    def _check_order(self):
        for shipment in self:
            if shipment.purchase_order_id.state != "purchase":
                raise ValidationError("只有已下单采购订单可以登记发货。")
            for line in shipment.line_ids:
                if line.purchase_order_line_id.order_id != shipment.purchase_order_id:
                    raise ValidationError("发货明细必须属于所选采购订单。")
            totals = {}
            for other in self.search(
                [
                    ("purchase_order_id", "=", shipment.purchase_order_id.id),
                    ("state", "!=", "cancelled"),
                ]
            ).mapped("line_ids"):
                order_line = other.purchase_order_line_id
                totals[order_line.id] = totals.get(order_line.id, 0) + other.uom_id._compute_quantity(
                    other.shipped_qty, order_line.product_uom
                )
            for order_line in shipment.purchase_order_id.order_line:
                if totals.get(order_line.id, 0) > order_line.product_qty + order_line.product_uom.rounding:
                    raise ValidationError(
                        f"{order_line.product_id.display_name} 累计发货数量不能超过订单数量。"
                    )


class BrewSupplierShipmentLine(models.Model):
    _name = "brew.supplier.shipment.line"
    _description = "Craft Brew Supplier Shipment Line"
    _check_company_auto = True

    shipment_id = fields.Many2one(
        "brew.supplier.shipment", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="shipment_id.company_id", store=True, index=True
    )
    purchase_order_line_id = fields.Many2one(
        "purchase.order.line", required=True, check_company=True
    )
    product_id = fields.Many2one(
        "product.product", related="purchase_order_line_id.product_id", store=True
    )
    shipped_qty = fields.Float(required=True)
    uom_id = fields.Many2one(
        "uom.uom", related="purchase_order_line_id.product_uom", store=True
    )

    _sql_constraints = [
        ("shipped_positive", "check(shipped_qty > 0)", "发货数量必须大于零。"),
    ]


class BrewReceipt(models.Model):
    _name = "brew.receipt"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Purchase Receipt"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="purchase_order_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="purchase_order_id.brew_factory_id", store=True, index=True
    )
    purchase_order_id = fields.Many2one(
        "purchase.order", required=True, check_company=True, index=True
    )
    shipment_id = fields.Many2one("brew.supplier.shipment", check_company=True, index=True)
    qc_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    destination_location_id = fields.Many2one(
        "stock.location", required=True, check_company=True
    )
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("pending_approval", "超收待审批"),
            ("arrived", "已到货待检"),
            ("inspected", "已质检"),
            ("done", "已入库"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many("brew.receipt.line", "receipt_id")
    picking_id = fields.Many2one("stock.picking", check_company=True, readonly=True)
    approval_instance_id = fields.Many2one(
        "brew.approval.instance", check_company=True, readonly=True
    )
    quality_inspection_id = fields.Many2one(
        "brew.quality.inspection", check_company=True, readonly=True
    )
    arrived_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.receipt") or "New"
        return super().create(values_list)

    def _over_receipt_metrics(self):
        self.ensure_one()
        max_rate = 0.0
        excess = 0.0
        for line in self.line_ids:
            order_line = line.purchase_order_line_id
            ordered = order_line.product_uom._compute_quantity(
                order_line.product_qty, order_line.product_id.uom_id
            )
            prior = sum(
                other.uom_id._compute_quantity(other.received_qty, other.product_id.uom_id)
                for other in self.env["brew.receipt.line"].search(
                    [
                        ("id", "!=", line.id),
                        ("purchase_order_line_id", "=", order_line.id),
                        ("receipt_id.state", "in", ["arrived", "inspected", "done"]),
                    ]
                )
            )
            current = line.uom_id._compute_quantity(
                line.received_qty, line.product_id.uom_id
            )
            line_excess = max(prior + current - ordered, 0)
            excess += line_excess
            max_rate = max(max_rate, line_excess / ordered if ordered else 1)
        return {"quantity": excess, "overage_rate": max_rate}

    def _over_receipt_requires_approval(self, metrics):
        self.ensure_one()
        if metrics["quantity"] <= 0:
            return False
        rules = self.env["brew.configuration.rule"].sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("rule_kind", "=", "approval"),
                ("code", "=", "purchase_overreceipt"),
                ("state", "=", "active"),
                "|",
                ("factory_id", "=", self.factory_id.id),
                ("factory_id", "=", False),
            ],
            order="factory_id desc, version desc",
        )
        if not rules:
            return True
        import json

        for rule in rules:
            conditions = json.loads(rule.config_json or "{}").get("conditions") or {}
            if all(
                float(metrics.get(key, 0)) >= float(value)
                for key, value in conditions.items()
            ):
                return True
        return False

    def action_confirm_arrival(self, user):
        self.ensure_one()
        if self.state not in {"draft", "pending_approval"} or not self.line_ids:
            raise ValidationError("当前收货单不能确认到货。")
        if self.purchase_order_id.state != "purchase":
            raise ValidationError("采购订单尚未正式下单。")
        metrics = self._over_receipt_metrics()
        if self.state == "draft" and self._over_receipt_requires_approval(metrics):
            approval = self.env["brew.approval.instance"].create_for_target(
                "purchase_overreceipt", self, user, self.factory_id, metrics
            )
            self.write(
                {"state": "pending_approval", "approval_instance_id": approval.id}
            )
            return False
        if (
            self.state == "pending_approval"
            and self.approval_instance_id.state != "approved"
        ):
            raise ValidationError("超收审批通过后才能确认到货。")
        picking_type = self.factory_id.stock_warehouse_id.in_type_id
        supplier_location = self.purchase_order_id.partner_id.property_stock_supplier
        if not picking_type or not supplier_location:
            raise ValidationError("厂区仓库或供应商收货库位未配置。")
        picking = self.env["stock.picking"].sudo().create(
            {
                "partner_id": self.purchase_order_id.partner_id.id,
                "picking_type_id": picking_type.id,
                "location_id": supplier_location.id,
                "location_dest_id": self.qc_location_id.id,
                "origin": self.name,
            }
        )
        inspection_commands = []
        for line in self.line_ids:
            lot = self.env["stock.lot"].sudo().search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("product_id", "=", line.product_id.id),
                    ("name", "=", line.lot_name),
                ],
                limit=1,
            )
            if not lot:
                lot = self.env["stock.lot"].sudo().create(
                    {
                        "company_id": self.company_id.id,
                        "product_id": line.product_id.id,
                        "name": line.lot_name,
                        "brew_supplier_lot": line.supplier_lot,
                        "brew_expiration_date": line.expiration_date,
                        "brew_quality_status": "pending",
                        "brew_source_model": self._name,
                        "brew_source_id": self.id,
                    }
                )
            elif lot.brew_source_model != self._name or lot.brew_source_id != self.id:
                raise ValidationError("系统批次码已被其他收货单使用，请生成新的批次码。")
            elif (
                lot.brew_expiration_date
                and line.expiration_date
                and lot.brew_expiration_date != line.expiration_date
            ):
                raise ValidationError("同一批次的效期信息不一致。")
            line.lot_id = lot
            self._brew_create_done_move(
                name=self.name,
                product=line.product_id,
                quantity=line.received_qty,
                uom=line.uom_id,
                source_location=supplier_location,
                destination_location=self.qc_location_id,
                lot=lot,
                source_model=self._name,
                source_id=self.id,
                source_reference=self.name,
                picking=picking,
            )
            inspection_commands.append(
                Command.create(
                    {
                        "receipt_line_id": line.id,
                        "product_id": line.product_id.id,
                        "lot_id": lot.id,
                        "inspected_qty": line.received_qty,
                        "qualified_qty": line.received_qty,
                        "rejected_qty": 0,
                        "uom_id": line.uom_id.id,
                    }
                )
            )
        picking.button_validate()
        inspection = self.env["brew.quality.inspection"].sudo().create(
            {
                "company_id": self.company_id.id,
                "factory_id": self.factory_id.id,
                "receipt_id": self.id,
                "line_ids": inspection_commands,
            }
        )
        self.write(
            {
                "state": "arrived",
                "picking_id": picking.id,
                "quality_inspection_id": inspection.id,
                "arrived_at": fields.Datetime.now(),
            }
        )
        if self.shipment_id:
            self.shipment_id.write(
                {"state": "arrived", "actual_arrival": fields.Datetime.now()}
            )
        return True

    def action_put_away(self):
        self.ensure_one()
        inspection = self.quality_inspection_id
        if (
            self.state != "inspected"
            or not inspection
            or inspection.state != "completed"
        ):
            raise ValidationError("来料质检完成后才能入库。")
        supplier_location = self.purchase_order_id.partner_id.property_stock_supplier
        for result in inspection.line_ids:
            if result.qualified_qty:
                self._brew_create_done_move(
                    name=self.name,
                    product=result.product_id,
                    quantity=result.qualified_qty,
                    uom=result.uom_id,
                    source_location=self.qc_location_id,
                    destination_location=self.destination_location_id,
                    lot=result.lot_id,
                    source_model=self._name,
                    source_id=self.id,
                    source_reference=self.name,
                )
            if result.rejected_qty:
                destination = (
                    supplier_location
                    if result.disposition == "return"
                    else result.product_id.with_company(
                        self.company_id
                    ).property_stock_inventory
                    if result.disposition == "scrap"
                    else False
                )
                if not destination:
                    raise ValidationError("不合格数量必须选择退货或报损处置。")
                self._brew_create_done_move(
                    name=self.name,
                    product=result.product_id,
                    quantity=result.rejected_qty,
                    uom=result.uom_id,
                    source_location=self.qc_location_id,
                    destination_location=destination,
                    lot=result.lot_id,
                    source_model=self._name,
                    source_id=self.id,
                    source_reference=self.name,
                )
            result.lot_id.write(
                {
                    "brew_quality_status": (
                        "qualified" if result.qualified_qty else "rejected"
                    )
                }
            )
        self.write({"state": "done", "completed_at": fields.Datetime.now()})
        return True


class BrewReceiptLine(models.Model):
    _name = "brew.receipt.line"
    _description = "Craft Brew Purchase Receipt Line"
    _check_company_auto = True

    receipt_id = fields.Many2one(
        "brew.receipt", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="receipt_id.company_id", store=True, index=True
    )
    purchase_order_line_id = fields.Many2one(
        "purchase.order.line", required=True, check_company=True
    )
    product_id = fields.Many2one(
        "product.product", related="purchase_order_line_id.product_id", store=True
    )
    received_qty = fields.Float(required=True)
    uom_id = fields.Many2one(
        "uom.uom", related="purchase_order_line_id.product_uom", store=True
    )
    lot_name = fields.Char(required=True)
    supplier_lot = fields.Char()
    expiration_date = fields.Date()
    lot_id = fields.Many2one("stock.lot", check_company=True, readonly=True)

    _sql_constraints = [
        ("received_positive", "check(received_qty > 0)", "收货数量必须大于零。"),
    ]

    @api.constrains("purchase_order_line_id", "receipt_id")
    def _check_order_line(self):
        for line in self:
            if line.purchase_order_line_id.order_id != line.receipt_id.purchase_order_id:
                raise ValidationError("收货明细必须属于所选采购订单。")


class BrewQualityInspection(models.Model):
    _name = "brew.quality.inspection"
    _description = "Craft Brew Incoming Quality Inspection"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one(
        "brew.factory", required=True, check_company=True, index=True
    )
    receipt_id = fields.Many2one(
        "brew.receipt",
        required=True,
        check_company=True,
        index=True,
        ondelete="restrict",
    )
    state = fields.Selection(
        [("draft", "待检"), ("completed", "已完成")],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many("brew.quality.inspection.line", "inspection_id")
    inspector_id = fields.Many2one("res.users", check_company=True, readonly=True)
    inspected_at = fields.Datetime(readonly=True)
    note = fields.Text()

    _sql_constraints = [
        ("receipt_unique", "unique(receipt_id)", "每张收货单只能有一张来料质检单。"),
    ]

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.quality.inspection")
                    or "New"
                )
        return super().create(values_list)

    def action_complete(self, user):
        self.ensure_one()
        if self.state != "draft" or not self.line_ids:
            raise ValidationError("当前质检单不能完成。")
        for line in self.line_ids:
            if (
                abs(line.qualified_qty + line.rejected_qty - line.inspected_qty)
                > line.uom_id.rounding
            ):
                raise ValidationError("合格与不合格数量之和必须等于检验数量。")
            if line.rejected_qty and not line.disposition:
                raise ValidationError("不合格数量必须选择处置方式。")
        self.write(
            {
                "state": "completed",
                "inspector_id": user.id,
                "inspected_at": fields.Datetime.now(),
            }
        )
        self.receipt_id.write({"state": "inspected"})
        return True


class BrewQualityInspectionLine(models.Model):
    _name = "brew.quality.inspection.line"
    _description = "Craft Brew Incoming Quality Inspection Line"
    _check_company_auto = True

    inspection_id = fields.Many2one(
        "brew.quality.inspection", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="inspection_id.company_id", store=True, index=True
    )
    receipt_line_id = fields.Many2one(
        "brew.receipt.line", required=True, ondelete="restrict"
    )
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    inspected_qty = fields.Float(required=True)
    qualified_qty = fields.Float(required=True)
    rejected_qty = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    disposition = fields.Selection(
        [("return", "退回供应商"), ("scrap", "报损")],
        index=True,
    )
    result_json = fields.Text(default="{}")
    note = fields.Text()

    _sql_constraints = [
        ("inspected_positive", "check(inspected_qty > 0)", "检验数量必须大于零。"),
        ("qualified_non_negative", "check(qualified_qty >= 0)", "合格数量不能小于零。"),
        ("rejected_non_negative", "check(rejected_qty >= 0)", "不合格数量不能小于零。"),
    ]

    @api.constrains(
        "inspected_qty", "qualified_qty", "rejected_qty", "product_id", "lot_id", "uom_id"
    )
    def _check_values(self):
        for line in self:
            if line.qualified_qty + line.rejected_qty > line.inspected_qty + line.uom_id.rounding:
                raise ValidationError("合格与不合格数量不能超过检验数量。")
            if line.lot_id.product_id != line.product_id:
                raise ValidationError("质检批次与产品不一致。")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("质检单位与产品单位类别不一致。")
