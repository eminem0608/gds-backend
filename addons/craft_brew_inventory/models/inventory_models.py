import json

from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


class BrewStockDocumentMixin(models.AbstractModel):
    _name = "brew.stock.document.mixin"
    _description = "Craft Brew Controlled Stock Movement Helper"

    def _brew_available_quantity(self, product, location, lot, excluded_source=None):
        available = self.env["stock.quant"]._get_available_quantity(
            product, location, lot_id=lot, strict=False, allow_negative=False
        )
        domain = [
            ("company_id", "=", self.env.company.id),
            ("product_id", "=", product.id),
            ("lot_id", "=", lot.id if lot else False),
            ("location_id", "child_of", location.id),
            ("state", "=", "active"),
        ]
        if excluded_source:
            source_model, source_id = excluded_source
            domain += ["|", ("source_model", "!=", source_model), ("source_id", "!=", source_id)]
        reservations = self.env["brew.inventory.reservation"].sudo().search(domain)
        reserved = sum(
            item.uom_id._compute_quantity(item.quantity, product.uom_id)
            for item in reservations
        )
        return available - reserved

    def _brew_create_done_move(
        self,
        *,
        name,
        product,
        quantity,
        uom,
        source_location,
        destination_location,
        lot=None,
        source_model,
        source_id,
        source_reference,
        picking=None,
    ):
        base_quantity = uom._compute_quantity(quantity, product.uom_id, round=False)
        if base_quantity <= 0:
            raise ValidationError("库存移动数量必须大于零。")
        if source_location.usage == "internal":
            available = self._brew_available_quantity(
                product, source_location, lot, (source_model, source_id)
            )
            if available + product.uom_id.rounding < base_quantity:
                lot_name = lot.name if lot else "无批次"
                raise ValidationError(
                    f"{product.display_name} 批次 {lot_name} 可用库存不足，禁止负库存。"
                )
        move = self.env["stock.move"].sudo().create(
            {
                "name": name,
                "picking_id": picking.id if picking else False,
                "product_id": product.id,
                "product_uom_qty": quantity,
                "product_uom": uom.id,
                "location_id": source_location.id,
                "location_dest_id": destination_location.id,
                "brew_controlled": True,
                "brew_source_model": source_model,
                "brew_source_id": source_id,
                "brew_source_reference": source_reference,
            }
        )
        move._action_confirm()
        move._action_assign()
        move_line = move.move_line_ids[:1]
        line_values = {
            "move_id": move.id,
            "picking_id": picking.id if picking else False,
            "product_id": product.id,
            "product_uom_id": product.uom_id.id,
            "location_id": source_location.id,
            "location_dest_id": destination_location.id,
            "lot_id": lot.id if lot else False,
            "quantity": base_quantity,
            "picked": True,
        }
        if move_line:
            move_line.write(line_values)
            (move.move_line_ids - move_line).unlink()
        else:
            self.env["stock.move.line"].sudo().create(line_values)
        if not picking:
            move._action_done()
            if move.state != "done":
                move._action_done()
        return move


class StockLot(models.Model):
    _inherit = "stock.lot"

    brew_supplier_lot = fields.Char(index=True)
    brew_expiration_date = fields.Date(index=True)
    brew_quality_status = fields.Selection(
        [("pending", "待检"), ("qualified", "合格"), ("rejected", "不合格")],
        required=True,
        default="pending",
        index=True,
    )
    brew_frozen = fields.Boolean(default=False, index=True)
    brew_freeze_reason = fields.Text()
    brew_source_model = fields.Char(index=True)
    brew_source_id = fields.Integer(index=True)
    brew_manual_packaging_id = fields.Many2one("product.packaging", ondelete="restrict")
    brew_manual_package_count = fields.Integer(default=0)

    def action_freeze(self, reason):
        if not str(reason or "").strip():
            raise ValidationError("冻结批次必须填写原因。")
        self.write({"brew_frozen": True, "brew_freeze_reason": reason})
        return True

    def action_unfreeze(self, reason):
        if not str(reason or "").strip():
            raise ValidationError("解冻批次必须填写原因。")
        self.write({"brew_frozen": False, "brew_freeze_reason": reason})
        return True


class StockMove(models.Model):
    _inherit = "stock.move"

    brew_source_model = fields.Char(index=True)
    brew_source_id = fields.Integer(index=True)
    brew_source_reference = fields.Char(index=True)
    brew_controlled = fields.Boolean(default=False, index=True)

    @api.constrains("brew_controlled", "brew_source_model", "brew_source_id")
    def _check_brew_source(self):
        for move in self:
            if move.brew_controlled and (not move.brew_source_model or not move.brew_source_id):
                raise ValidationError("受控库存移动必须关联来源单据。")


class BrewProductionPlan(models.Model):
    _inherit = "brew.production.plan"

    def action_dispatch(self):
        for plan in self:
            snapshot = json.loads(plan.material_snapshot_json or "{}")
            materials = snapshot.get("materials", []) if isinstance(snapshot, dict) else []
            reservations = self.env["brew.inventory.reservation"].sudo().search(
                [
                    ("company_id", "=", plan.company_id.id),
                    ("source_model", "=", plan._name),
                    ("source_id", "=", plan.id),
                    ("state", "=", "active"),
                ]
            )
            totals = {}
            for reservation in reservations:
                totals[reservation.product_id.id] = (
                    totals.get(reservation.product_id.id, 0)
                    + reservation.quantity_base
                )
            for material in materials:
                product = self.env["product.product"].browse(
                    int(material.get("material_id") or 0)
                )
                uom = self.env["uom.uom"].browse(int(material.get("uom_id") or 0))
                required = uom._compute_quantity(
                    float(material.get("quantity") or 0), product.uom_id
                )
                if totals.get(product.id, 0) + product.uom_id.rounding < required:
                    raise ValidationError(
                        f"{product.display_name} 尚未足量预占，生产计划不能下发。"
                    )
        return super().action_dispatch()

    def action_cancel(self, reason):
        result = super().action_cancel(reason)
        self.env["brew.inventory.reservation"].sudo().search(
            [
                ("source_model", "=", self._name),
                ("source_id", "in", self.ids),
                ("state", "=", "active"),
            ]
        ).action_release()
        return result

    def action_terminate(self, reason):
        result = super().action_terminate(reason)
        self.env["brew.inventory.reservation"].sudo().search(
            [
                ("source_model", "=", self._name),
                ("source_id", "in", self.ids),
                ("state", "=", "active"),
            ]
        ).action_release()
        return result


class BrewInventoryReservation(models.Model):
    _name = "brew.inventory.reservation"
    _description = "Craft Brew Lot Reservation"
    _order = "create_date, id"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True, index=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True, index=True)
    location_id = fields.Many2one("stock.location", required=True, check_company=True, index=True)
    quantity = fields.Float(required=True)
    quantity_base = fields.Float(compute="_compute_quantity_base", store=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    source_model = fields.Char(required=True, index=True)
    source_id = fields.Integer(required=True, index=True)
    state = fields.Selection(
        [("active", "有效"), ("consumed", "已耗用"), ("released", "已释放"), ("cancelled", "已取消")],
        required=True,
        default="active",
        index=True,
    )
    expires_at = fields.Datetime(index=True)

    _sql_constraints = [
        ("quantity_positive", "check(quantity > 0)", "预占数量必须大于零。"),
    ]

    @api.depends("quantity", "uom_id", "product_id")
    def _compute_quantity_base(self):
        for record in self:
            record.quantity_base = (
                record.uom_id._compute_quantity(record.quantity, record.product_id.uom_id, round=False)
                if record.uom_id and record.product_id
                else 0
            )

    @api.constrains("uom_id", "product_id", "lot_id")
    def _check_relations(self):
        for record in self:
            if record.uom_id.category_id != record.product_id.uom_id.category_id:
                raise ValidationError("预占单位与产品单位类别不一致。")
            if record.lot_id.product_id != record.product_id:
                raise ValidationError("预占批次与产品不一致。")

    def action_release(self):
        self.filtered(lambda item: item.state == "active").write({"state": "released"})
        return True

    @api.model
    def release_expired(self):
        expired = self.search(
            [("state", "=", "active"), ("expires_at", "!=", False), ("expires_at", "<=", fields.Datetime.now())]
        )
        expired.write({"state": "released"})
        return len(expired)


class BrewInventoryTransfer(models.Model):
    _name = "brew.inventory.transfer"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Inventory Transfer"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    source_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    destination_location_id = fields.Many2one("stock.location", required=True, check_company=True)
    reason = fields.Text(required=True)
    state = fields.Selection([("draft", "草稿"), ("done", "已完成"), ("cancelled", "已取消")], required=True, default="draft", index=True)
    line_ids = fields.One2many("brew.inventory.transfer.line", "transfer_id")
    picking_id = fields.Many2one("stock.picking", check_company=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New": values["name"] = self.env["ir.sequence"].next_by_code("brew.inventory.transfer") or "New"
        return super().create(values_list)

    @api.constrains("source_location_id", "destination_location_id", "factory_id")
    def _check_locations(self):
        for transfer in self:
            if transfer.source_location_id == transfer.destination_location_id: raise ValidationError("来源和目标库位不能相同。")
            if transfer.source_location_id.brew_factory_id != transfer.factory_id or transfer.destination_location_id.brew_factory_id != transfer.factory_id: raise ValidationError("调拨库位必须属于所选厂区。")
            if "material_issue" in (transfer.source_location_id.brew_location_type, transfer.destination_location_id.brew_location_type):
                raise ValidationError("领料库物料请通过领料单办理发料或退料。")

    def action_confirm(self):
        self.ensure_one()
        if self.state != "draft" or not self.line_ids:
            raise ValidationError("只有包含明细的草稿调拨单可以确认。")
        picking_type = self.factory_id.stock_warehouse_id.int_type_id
        if not picking_type:
            raise ValidationError("厂区仓库未配置内部调拨类型。")
        picking = self.env["stock.picking"].sudo().create(
            {
                "picking_type_id": picking_type.id,
                "location_id": self.source_location_id.id,
                "location_dest_id": self.destination_location_id.id,
                "origin": self.name,
            }
        )
        for line in self.line_ids:
            self._brew_create_done_move(
                name=self.name,
                product=line.product_id,
                quantity=line.quantity,
                uom=line.uom_id,
                source_location=self.source_location_id,
                destination_location=self.destination_location_id,
                lot=line.lot_id,
                source_model=self._name,
                source_id=self.id,
                source_reference=self.name,
                picking=picking,
            )
        picking.button_validate()
        self.write({"state": "done", "picking_id": picking.id})
        return True


class BrewInventoryTransferLine(models.Model):
    _name = "brew.inventory.transfer.line"
    _description = "Craft Brew Inventory Transfer Line"

    transfer_id = fields.Many2one("brew.inventory.transfer", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="transfer_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    _sql_constraints = [("quantity_positive", "check(quantity > 0)", "调拨数量必须大于零。")]

    @api.constrains("product_id", "lot_id", "uom_id")
    def _check_relations(self):
        for line in self:
            if line.lot_id.product_id != line.product_id:
                raise ValidationError("调拨批次与产品不一致。")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("调拨单位与产品单位类别不一致。")


class BrewInventoryCount(models.Model):
    _name = "brew.inventory.count"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Inventory Count"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    location_id = fields.Many2one("stock.location", required=True, check_company=True)
    factory_id = fields.Many2one(
        "brew.factory", related="location_id.brew_factory_id", store=True, index=True
    )
    state = fields.Selection([("draft", "草稿"), ("counting", "盘点中"), ("pending_approval", "待审批"), ("done", "已完成"), ("cancelled", "已取消")], default="draft", required=True, index=True)
    line_ids = fields.One2many("brew.inventory.count.line", "count_id")
    approval_instance_id = fields.Many2one("brew.approval.instance", check_company=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New": values["name"] = self.env["ir.sequence"].next_by_code("brew.inventory.count") or "New"
        return super().create(values_list)

    def action_start(self):
        self.ensure_one()
        if self.state != "draft":
            raise ValidationError("只有草稿盘点单可以开始。")
        # Serialize starts within the same factory, including overlapping parent scopes.
        self.env.cr.execute("SELECT id FROM brew_factory WHERE id = %s FOR UPDATE", [self.factory_id.id])
        active_counts = self.search([
            ('id', '!=', self.id), ('company_id', '=', self.company_id.id),
            ('factory_id', '=', self.factory_id.id), ('state', 'in', ['counting', 'pending_approval'])])
        descendants = self.env['stock.location'].search([('id', 'child_of', self.location_id.id)]).ids
        active = any(count.location_id.id in descendants or
                     self.location_id.id in self.env['stock.location'].search([('id', 'child_of', count.location_id.id)]).ids
                     for count in active_counts)
        if active:
            raise ValidationError("该库位范围已有进行中的盘点单。")
        quants = self.env["stock.quant"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("location_id", "child_of", self.location_id.id),
                ("location_id.usage", "=", "internal"),
                ("location_id.brew_factory_id", "=", self.factory_id.id),
                ("quantity", "!=", 0),
            ]
        )
        if not quants:
            raise ValidationError("所选库位及其子库位没有账面库存，请选择有库存的库位。")
        totals = {}
        for quant in quants:
            key = (quant.location_id.id, quant.product_id.id, quant.lot_id.id or False)
            totals[key] = totals.get(key, 0) + quant.quantity
        previous_by_key = {}
        previous_lines = self.env["brew.inventory.count.line"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("count_id.state", "=", "done"),
                ("location_id", "child_of", self.location_id.id),
            ],
            order="create_date desc, id desc",
        )
        for line in previous_lines:
            key = (line.location_id.id, line.product_id.id, line.lot_id.id or False)
            previous_by_key.setdefault(key, line)
        commands = [Command.clear()]
        for (location_id, product_id, lot_id), quantity in totals.items():
            product = self.env["product.product"].browse(product_id)
            previous_line = previous_by_key.get((location_id, product_id, lot_id))
            commands.append(
                Command.create(
                    {
                        "location_id": location_id,
                        "product_id": product_id,
                        "lot_id": lot_id,
                        "theoretical_qty": quantity,
                        "counted_qty": quantity,
                        "uom_id": product.uom_id.id,
                        "previous_count_line_id": previous_line.id if previous_line else False,
                    }
                )
            )
        self.write({"state": "counting", "line_ids": commands})
        return True

    def action_submit(self, user):
        self.ensure_one()
        if self.state != "counting": raise ValidationError("只有盘点中单据可以提交。")
        if self.line_ids.filtered(
            lambda line: abs(line.difference_qty) > 0.000001 and not line.difference_reason
        ):
            raise ValidationError("存在盘点差异时必须填写差异原因。")
        difference = sum(abs(line.difference_qty) for line in self.line_ids)
        theoretical = sum(abs(line.theoretical_qty) for line in self.line_ids)
        rate = difference / theoretical if theoretical else (1 if difference else 0)
        approval = self.env["brew.approval.instance"].create_for_target(
            "inventory_count",
            self,
            user,
            self.factory_id,
            {"quantity": difference, "difference_rate": rate},
        )
        self.write({"state": "pending_approval", "approval_instance_id": approval.id})
        return True

    def action_confirm(self):
        self.ensure_one()
        if (
            self.state != "pending_approval"
            or not self.approval_instance_id
            or self.approval_instance_id.state != "approved"
        ):
            raise ValidationError("盘点差异审批通过后才能确认。")
        for line in self.line_ids.filtered(lambda item: item.difference_qty):
            inventory_location = line.product_id.with_company(
                self.company_id
            ).property_stock_inventory
            if not inventory_location:
                raise ValidationError(f"{line.product_id.display_name} 未配置盘点损益库位。")
            source = inventory_location if line.difference_qty > 0 else line.location_id
            destination = line.location_id if line.difference_qty > 0 else inventory_location
            self._brew_create_done_move(
                name=self.name,
                product=line.product_id,
                quantity=abs(line.difference_qty),
                uom=line.uom_id,
                source_location=source,
                destination_location=destination,
                lot=line.lot_id,
                source_model=self._name,
                source_id=self.id,
                source_reference=self.name,
            )
        self.write({"state": "done"})
        return True


class BrewInventoryCountLine(models.Model):
    _name = "brew.inventory.count.line"
    _description = "Craft Brew Inventory Count Line"

    count_id = fields.Many2one("brew.inventory.count", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="count_id.company_id", store=True, index=True)
    location_id = fields.Many2one("stock.location", required=True, check_company=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", check_company=True)
    theoretical_qty = fields.Float(readonly=True)
    counted_qty = fields.Float(required=True)
    difference_qty = fields.Float(compute="_compute_difference", store=True)
    previous_count_line_id = fields.Many2one(
        "brew.inventory.count.line", readonly=True, ondelete="set null"
    )
    difference_reason = fields.Selection(
        [
            ("damage", "破损"),
            ("expired", "过期报废"),
            ("measurement", "计量误差"),
            ("missing_transaction", "单据未及时入账"),
            ("unrecorded_issue_return", "领料或退料未登记"),
            ("other", "其他"),
        ],
        string="差异原因",
    )
    difference_note = fields.Char("差异说明")
    uom_id = fields.Many2one("uom.uom", required=True)

    @api.depends("theoretical_qty", "counted_qty")
    def _compute_difference(self):
        for line in self: line.difference_qty = line.counted_qty - line.theoretical_qty

    @api.constrains(
        "counted_qty",
        "product_id",
        "lot_id",
        "uom_id",
        "difference_reason",
        "difference_note",
    )
    def _check_count_line(self):
        for line in self:
            if line.counted_qty < 0:
                raise ValidationError("盘点实数不能小于零。")
            if line.product_id.tracking != "none" and not line.lot_id:
                raise ValidationError("按批次管理的产品必须填写批次。")
            if line.lot_id and line.lot_id.product_id != line.product_id:
                raise ValidationError("盘点批次与产品不一致。")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("盘点单位与产品单位类别不一致。")
            has_difference = abs(line.counted_qty - line.theoretical_qty) > 0.000001
            if has_difference and not line.difference_reason:
                raise ValidationError("存在盘点差异时必须填写差异原因。")
            if has_difference and line.difference_reason == "other" and not str(
                line.difference_note or ""
            ).strip():
                raise ValidationError("差异原因为其他时必须填写差异说明。")


class BrewInventoryScrap(models.Model):
    _name = "brew.inventory.scrap"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Inventory Scrap"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    location_id = fields.Many2one("stock.location", required=True, check_company=True)
    reason = fields.Text(required=True)
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("pending_approval", "待审批"),
            ("approved", "已批准"),
            ("done", "已完成"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many("brew.inventory.scrap.line", "scrap_id")
    approval_instance_id = fields.Many2one(
        "brew.approval.instance", check_company=True, readonly=True
    )
    attachment_ids = fields.Many2many("ir.attachment", string="照片/附件")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.inventory.scrap") or "New"
                )
        return super().create(values_list)

    @api.constrains("location_id", "factory_id")
    def _check_location(self):
        for document in self:
            if document.location_id.brew_factory_id != document.factory_id:
                raise ValidationError("报损库位必须属于所选厂区。")

    def action_submit(self, user):
        self.ensure_one()
        if self.state != "draft" or not self.line_ids:
            raise ValidationError("只有包含明细的草稿报损单可以提交。")
        quantity = sum(
            line.uom_id._compute_quantity(line.quantity, line.product_id.uom_id)
            for line in self.line_ids
        )
        approval = self.env["brew.approval.instance"].create_for_target(
            "inventory_scrap",
            self,
            user,
            self.factory_id,
            {"quantity": quantity},
        )
        self.write(
            {
                "state": "pending_approval",
                "approval_instance_id": approval.id,
            }
        )
        return approval

    def action_confirm(self):
        self.ensure_one()
        if self.state == "pending_approval" and self.approval_instance_id.state == "approved":
            self.write({"state": "approved"})
        if self.state != "approved":
            raise ValidationError("报损审批通过后才能确认。")
        for line in self.line_ids:
            scrap_location = line.product_id.with_company(
                self.company_id
            ).property_stock_inventory
            if not scrap_location:
                raise ValidationError(f"{line.product_id.display_name} 未配置损耗库位。")
            self._brew_create_done_move(
                name=self.name,
                product=line.product_id,
                quantity=line.quantity,
                uom=line.uom_id,
                source_location=self.location_id,
                destination_location=scrap_location,
                lot=line.lot_id,
                source_model=self._name,
                source_id=self.id,
                source_reference=self.name,
            )
        self.write({"state": "done"})
        return True


class BrewInventoryScrapLine(models.Model):
    _name = "brew.inventory.scrap.line"
    _description = "Craft Brew Inventory Scrap Line"

    scrap_id = fields.Many2one(
        "brew.inventory.scrap", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="scrap_id.company_id", store=True, index=True
    )
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)

    _sql_constraints = [
        ("quantity_positive", "check(quantity > 0)", "报损数量必须大于零。"),
    ]

    @api.constrains("product_id", "lot_id", "uom_id")
    def _check_relations(self):
        for line in self:
            if line.lot_id.product_id != line.product_id:
                raise ValidationError("报损批次与产品不一致。")
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("报损单位与产品单位类别不一致。")
