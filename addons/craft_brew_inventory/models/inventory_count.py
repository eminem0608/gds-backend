"""Physical counts: snapshot scopes, explicit counts and atomic stock adjustments."""
import json
import math
import uuid

from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero


class InventoryCount(models.Model):
    _inherit = "brew.inventory.count"

    owner_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    completed_by_id = fields.Many2one("res.users", readonly=True)
    scope_locked = fields.Boolean(default=False, readonly=True, index=True)
    cancel_reason = fields.Text(readonly=True)
    history_json = fields.Text(default="[]", readonly=True)
    revision_token = fields.Char(default=lambda self: uuid.uuid4().hex, readonly=True)

    def init(self):
        self.env.cr.execute("""CREATE TABLE IF NOT EXISTS brew_inventory_count_guard (
            company_id integer PRIMARY KEY REFERENCES res_company(id) ON DELETE CASCADE,
            revision bigint NOT NULL DEFAULT 0)""")

    def write(self, values):
        values = dict(values, revision_token=uuid.uuid4().hex)
        return super().write(values)

    @api.model
    def _lock_stock(self, company_ids):
        # One short transaction lock per company, shared by snapshots, quant
        # writes and reservations. No session locks survive a commit/rollback.
        for company_id in sorted(set(company_ids)):
            self.env.cr.execute("SELECT pg_advisory_xact_lock(771812, %s)", [company_id])
            # Odoo uses REPEATABLE READ. Updating a shared row also forces an
            # automatic transaction retry when a waiter has an older snapshot.
            self.env.cr.execute("""INSERT INTO brew_inventory_count_guard (company_id, revision)
                VALUES (%s, 1) ON CONFLICT (company_id)
                DO UPDATE SET revision = brew_inventory_count_guard.revision + 1""", [company_id])

    @api.model
    def _frozen_locations(self, company_id):
        counts = self.sudo().search([
            ("company_id", "=", company_id), ("scope_locked", "=", True),
            ("state", "in", ["counting", "pending_approval"]),
        ])
        return self.env["stock.location"].sudo().search([
            ("id", "child_of", counts.mapped("location_id").ids), ("usage", "=", "internal"),
        ]) if counts else self.env["stock.location"]

    @api.model
    def _assert_locations_open(self, locations, company_id):
        locations = locations.filtered(lambda loc: loc.usage == "internal")
        if not locations:
            return
        self._lock_stock([company_id])
        counts = self.sudo().search([
            ("company_id", "=", company_id), ("scope_locked", "=", True),
            ("state", "in", ["counting", "pending_approval"]),
        ])
        allowed = self.env.context.get("brew_count_posting_id")
        for count in counts:
            if (allowed == count.id and count.state == "pending_approval"
                    and count.approval_instance_id.state == "approved"):
                continue
            scope = self.env["stock.location"].sudo().search([("id", "child_of", count.location_id.id)])
            if locations & scope:
                raise ValidationError(
                    f"{count.location_id.complete_name} 正在盘点（{count.name}），暂不能收发或新增预占。请先完成或取消该盘点单。"
                )

    def _locked(self):
        self.ensure_one()
        self._lock_stock(self.company_id.ids)
        self.env.cr.execute("SELECT id FROM brew_inventory_count WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()
        self.line_ids.invalidate_recordset()
        return self

    def _log_count(self, user, action, detail=""):
        self.ensure_one()
        history = json.loads(self.history_json or "[]")
        history.append({"action": action, "by": user.name, "by_id": user.id,
                        "at": fields.Datetime.now().isoformat(), "detail": detail})
        self.write({"history_json": json.dumps(history, ensure_ascii=False)})

    def _editable_count(self):
        self.ensure_one()
        return self.state == "counting" or (
            self.state == "pending_approval" and self.approval_instance_id.state == "returned"
        )

    def _snapshot_quantities(self):
        self.ensure_one()
        quantities = {}
        quants = self.env["stock.quant"].sudo().search([
            ("company_id", "=", self.company_id.id),
            ("location_id", "child_of", self.location_id.id),
            ("location_id.usage", "=", "internal"),
            ("location_id.brew_factory_id", "=", self.factory_id.id),
        ])
        quants.invalidate_recordset(["quantity", "reserved_quantity"])
        for quant in quants:
            key = (quant.location_id.id, quant.product_id.id, quant.lot_id.id or False)
            quantities[key] = quantities.get(key, 0) + quant.quantity
        return quantities

    def _validate_snapshot(self):
        self.ensure_one()
        if not self.scope_locked:
            raise ValidationError("该历史盘点单没有有效冻结快照，请取消后重新新建盘点。")
        current = self._snapshot_quantities()
        expected = {(line.location_id.id, line.product_id.id, line.lot_id.id or False): line.theoretical_qty
                    for line in self.line_ids}
        for key in current.keys() | expected.keys():
            product = self.env["product.product"].browse(key[1])
            if float_compare(current.get(key, 0), expected.get(key, 0), precision_rounding=product.uom_id.rounding):
                raise ValidationError("盘点期间账面库存发生变化，禁止用旧快照入账。请核查后取消本单并重新盘点。")

    def action_start(self):
        self._locked()
        if self.state != "draft":
            raise ValidationError("只有草稿盘点单可以开始。")
        if self.location_id.usage != "internal" or not self.factory_id or self.factory_id.company_id != self.company_id:
            raise ValidationError("请选择属于本公司厂区的内部库位。")
        active = self.sudo().search([
            ("id", "!=", self.id), ("company_id", "=", self.company_id.id),
            ("state", "in", ["counting", "pending_approval"]),
        ])
        scope = self.env["stock.location"].sudo().search([("id", "child_of", self.location_id.id)])
        for count in active:
            if count.location_id in scope or self.location_id in self.env["stock.location"].sudo().search([("id", "child_of", count.location_id.id)]):
                raise ValidationError(f"该范围与进行中的盘点单 {count.name} 重叠，请先完成或取消原盘点。")
        if scope.filtered(lambda loc: loc.usage == "internal" and loc.brew_factory_id != self.factory_id):
            raise ValidationError("盘点范围包含未归属本厂区的子库位，请先修正库位归属。")
        previous = {}
        for line in self.env["brew.inventory.count.line"].sudo().search([
            ("company_id", "=", self.company_id.id), ("count_id.state", "=", "done"),
            ("location_id", "child_of", self.location_id.id),
        ], order="count_id desc, id desc"):
            previous.setdefault((line.location_id.id, line.product_id.id, line.lot_id.id or False), line.id)
        commands = [Command.clear()]
        for key, quantity in self._snapshot_quantities().items():
            location_id, product_id, lot_id = key
            product = self.env["product.product"].browse(product_id)
            if float_is_zero(quantity, precision_rounding=product.uom_id.rounding):
                continue
            commands.append(Command.create({
                "location_id": location_id, "product_id": product_id, "lot_id": lot_id,
                "theoretical_qty": quantity, "counted_qty": 0, "is_counted": False,
                "uom_id": product.uom_id.id, "previous_count_line_id": previous.get(key),
            }))
        self.write({"state": "counting", "scope_locked": True,
                    "started_at": fields.Datetime.now(), "line_ids": commands})
        self._log_count(self.env.user, "开始盘点", "生成账面快照，暂停本范围收发")
        return True

    def _validate_counted(self):
        self.ensure_one()
        if self.line_ids.filtered(lambda line: not line.is_counted):
            raise ValidationError("还有未盘明细，实盘为零时请明确填写 0。")
        for line in self.line_ids:
            if not float_is_zero(line.difference_qty, precision_rounding=line.uom_id.rounding):
                if not line.difference_reason:
                    raise ValidationError(f"{line.product_id.display_name} 存在差异，请填写差异原因。")
                if line.difference_reason == "other" and not str(line.difference_note or "").strip():
                    raise ValidationError("差异原因为其他时必须填写说明。")
                reserved = self.env["brew.inventory.reservation"].sudo().search([
                    ("company_id", "=", self.company_id.id), ("state", "=", "active"),
                    ("location_id", "=", line.location_id.id), ("product_id", "=", line.product_id.id),
                    ("lot_id", "=", line.lot_id.id or False),
                ])
                native = sum(self.env["stock.quant"].sudo().search([
                    ("company_id", "=", self.company_id.id), ("location_id", "=", line.location_id.id),
                    ("product_id", "=", line.product_id.id), ("lot_id", "=", line.lot_id.id or False),
                ]).mapped("reserved_quantity"))
                if float_compare(line.counted_qty, native + sum(reserved.mapped("quantity_base")), precision_rounding=line.uom_id.rounding) < 0:
                    raise ValidationError(f"{line.product_id.display_name} 实盘数量少于已有预占，请先处理受影响计划的预占。")

    def action_submit(self, user):
        self._locked()
        if not self._editable_count():
            raise ValidationError("只有盘点中或审批退回的单据可以提交。")
        self._validate_snapshot()
        self._validate_counted()
        differences = self.line_ids.filtered(lambda line: not float_is_zero(line.difference_qty, precision_rounding=line.uom_id.rounding))
        if not differences:
            self.write({"state": "done", "scope_locked": False, "completed_at": fields.Datetime.now(), "completed_by_id": user.id})
            self._log_count(user, "盘点完成", "账实一致，无需库存调整")
            return True
        # Quantity totals are meaningless across kg, L and units. Rules use the
        # maximum line difference rate and the number of discrepant lines.
        rate = max(abs(line.difference_qty) / abs(line.theoretical_qty) if line.theoretical_qty else 1 for line in differences)
        approval = self.env["brew.approval.instance"].create_for_target(
            "inventory_count", self, user, self.factory_id,
            {"difference_rate": rate, "difference_lines": len(differences), "quantity": len(differences)},
        )
        self.write({"state": "pending_approval", "approval_instance_id": approval.id})
        self._log_count(user, "提交差异审批", f"{len(differences)} 项差异")
        return True

    def action_confirm(self):
        self._locked()
        if self.state != "pending_approval" or self.approval_instance_id.state != "approved":
            raise ValidationError("盘点差异审批通过后才能入账，已入账单据不能重复操作。")
        self._validate_snapshot()
        self._validate_counted()
        for line in self.line_ids.filtered(lambda item: not float_is_zero(item.difference_qty, precision_rounding=item.uom_id.rounding)):
            inventory_location = line.product_id.with_company(self.company_id).property_stock_inventory
            if not inventory_location:
                raise ValidationError(f"{line.product_id.display_name} 未配置盘点损益库位。")
            self.with_context(brew_count_posting_id=self.id)._brew_create_done_move(
                name=self.name, product=line.product_id, quantity=abs(line.difference_qty), uom=line.uom_id,
                source_location=inventory_location if line.difference_qty > 0 else line.location_id,
                destination_location=line.location_id if line.difference_qty > 0 else inventory_location,
                lot=line.lot_id, source_model=self._name, source_id=self.id, source_reference=self.name,
            )
        self.write({"state": "done", "scope_locked": False, "completed_at": fields.Datetime.now(), "completed_by_id": self.env.user.id})
        self._log_count(self.env.user, "差异入账", "生成库存调整流水，恢复本范围收发")
        return True

    def action_cancel(self, user, reason):
        self._locked()
        if self.state in {"done", "cancelled"} or not str(reason or "").strip():
            raise ValidationError("仅未完成的盘点单可取消，且必须填写原因。")
        pending = self.env["brew.approval.instance"].sudo().search([
            ("target_model", "=", self._name), ("target_id", "=", self.id), ("state", "=", "pending"),
        ])
        pending.write({"state": "cancelled", "completed_at": fields.Datetime.now()})
        self.write({"state": "cancelled", "scope_locked": False, "cancel_reason": reason.strip()})
        self._log_count(user, "取消盘点", reason.strip())
        return True


class InventoryCountLine(models.Model):
    _inherit = "brew.inventory.count.line"

    is_counted = fields.Boolean(default=False)
    counted_at = fields.Datetime(readonly=True)
    counted_by_id = fields.Many2one("res.users", readonly=True)
    recounts_json = fields.Text(default="[]", readonly=True)

    _sql_constraints = [("count_scope_unique", "UNIQUE NULLS NOT DISTINCT (count_id, location_id, product_id, lot_id)",
                         "同一库位、商品和批次不能重复添加到盘点单。")]

    @api.depends("theoretical_qty", "counted_qty", "is_counted")
    def _compute_difference(self):
        for line in self:
            line.difference_qty = line.counted_qty - line.theoretical_qty if line.is_counted else 0

    @api.constrains("counted_qty", "product_id", "lot_id", "uom_id", "location_id", "count_id", "is_counted")
    def _check_count_line(self):
        # Draft saves may contain incomplete reasons; enforce those at submit.
        for line in self:
            if not math.isfinite(line.counted_qty) or line.counted_qty < 0:
                raise ValidationError("盘点实数必须为大于等于零的有限数字。")
            if line.product_id.tracking != "none" and not line.lot_id:
                raise ValidationError("按批次管理的产品必须填写批次。")
            if line.lot_id and line.lot_id.product_id != line.product_id:
                raise ValidationError("盘点批次与产品不一致。")
            if line.uom_id != line.product_id.uom_id:
                raise ValidationError("盘点必须使用商品的库存基础单位。")
            scope = self.env["stock.location"].search([("id", "child_of", line.count_id.location_id.id)])
            if line.location_id not in scope or line.location_id.usage != "internal" or line.location_id.brew_factory_id != line.count_id.factory_id:
                raise ValidationError("盘点明细库位必须属于本单范围及厂区。")


class CountStockMove(models.Model):
    _inherit = "stock.move"

    def _action_assign(self, *args, **kwargs):
        counts = self.env["brew.inventory.count"]
        counts._lock_stock(self.mapped("company_id").ids)
        for company in self.mapped("company_id"):
            counts._assert_locations_open(self.filtered(lambda move: move.company_id == company).mapped("location_id"), company.id)
        return super()._action_assign(*args, **kwargs)

    def _action_done(self, *args, **kwargs):
        counts = self.env["brew.inventory.count"]
        counts._lock_stock(self.mapped("company_id").ids)
        for company in self.mapped("company_id"):
            moves = self.filtered(lambda move: move.company_id == company)
            locations = moves.mapped("location_id") | moves.mapped("location_dest_id") | moves.move_line_ids.mapped("location_id") | moves.move_line_ids.mapped("location_dest_id")
            counts._assert_locations_open(locations, company.id)
        return super()._action_done(*args, **kwargs)


class CountStockQuant(models.Model):
    _inherit = "stock.quant"

    @api.model
    def _update_available_quantity(self, product_id, location_id, *args, **kwargs):
        self.env["brew.inventory.count"]._assert_locations_open(location_id, location_id.company_id.id or self.env.company.id)
        return super()._update_available_quantity(product_id, location_id, *args, **kwargs)

    @api.model_create_multi
    def create(self, values_list):
        counts = self.env["brew.inventory.count"]
        for values in values_list:
            if values.get("quantity", 0) or values.get("reserved_quantity", 0):
                loc = self.env["stock.location"].browse(values.get("location_id"))
                counts._assert_locations_open(loc, loc.company_id.id or self.env.company.id)
        return super().create(values_list)

    def write(self, values):
        if any(key in values for key in ("quantity", "location_id", "lot_id", "product_id", "company_id", "reserved_quantity")):
            for quant in self:
                increases_reservation = "reserved_quantity" in values and values["reserved_quantity"] > quant.reserved_quantity
                changes_stock = any(key in values for key in ("quantity", "location_id", "lot_id", "product_id", "company_id"))
                if changes_stock or increases_reservation:
                    loc = quant.location_id | self.env["stock.location"].browse(values.get("location_id", []))
                    self.env["brew.inventory.count"]._assert_locations_open(loc, quant.company_id.id or self.env.company.id)
        return super().write(values)

    def unlink(self):
        for quant in self.filtered(lambda q: q.quantity or q.reserved_quantity):
            self.env["brew.inventory.count"]._assert_locations_open(quant.location_id, quant.company_id.id or self.env.company.id)
        return super().unlink()


class CountReservation(models.Model):
    _inherit = "brew.inventory.reservation"

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("state", "active") == "active":
                loc = self.env["stock.location"].browse(values.get("location_id"))
                self.env["brew.inventory.count"]._assert_locations_open(loc, values.get("company_id") or self.env.company.id)
        return super().create(values_list)

    def write(self, values):
        if any(key in values for key in ("quantity", "location_id", "state", "product_id", "lot_id")):
            for reservation in self:
                if values.get("state", reservation.state) == "active":
                    loc = self.env["stock.location"].browse(values.get("location_id", reservation.location_id.id))
                    self.env["brew.inventory.count"]._assert_locations_open(loc, reservation.company_id.id)
        return super().write(values)
