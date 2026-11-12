import json
import math

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class BrewMaterialRequisition(models.Model):
    _name = "brew.material.requisition"
    _inherit = "brew.stock.document.mixin"
    _description = "Craft Brew Material Requisition"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="production_plan_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="production_plan_id.factory_id", store=True, index=True
    )
    production_plan_id = fields.Many2one(
        "brew.production.plan", required=True, check_company=True, index=True
    )
    source_location_id = fields.Many2one(
        "stock.location", required=True, check_company=True
    )
    production_location_id = fields.Many2one(
        "stock.location", required=True, check_company=True
    )
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("overage_pending", "超领待审批"),
            ("recommended", "已推荐批次"),
            ("issued", "已发料"),
            ("acknowledged", "已确认实领"),
            ("completed", "已完成"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many("brew.material.requisition.line", "requisition_id")
    overage_reason = fields.Text()
    approval_instance_id = fields.Many2one(
        "brew.approval.instance", check_company=True, readonly=True
    )
    issued_at = fields.Datetime(readonly=True)
    acknowledged_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.material.requisition")
                    or "New"
                )
        return super().create(values_list)

    @api.constrains("source_location_id", "production_location_id", "factory_id")
    def _check_locations(self):
        for document in self:
            if document.source_location_id == document.production_location_id:
                raise ValidationError("发料库位与生产暂存库位不能相同。")
            if (
                document.source_location_id.brew_factory_id != document.factory_id
                or document.production_location_id.brew_factory_id != document.factory_id
            ):
                raise ValidationError("领料库位必须属于计划厂区。")

    def overage_metrics(self):
        self.ensure_one()
        excess = sum(max(line.requested_qty - line.planned_qty, 0) for line in self.line_ids)
        planned = sum(self.line_ids.mapped("planned_qty"))
        return {
            "quantity": excess,
            "overage_rate": excess / planned if planned else (1 if excess else 0),
        }

    def action_request_overage(self, user, reason):
        self.ensure_one()
        metrics = self.overage_metrics()
        if self.state != "draft" or metrics["quantity"] <= 0 or not str(reason or "").strip():
            raise ValidationError("只有存在超领的草稿领料单可申请审批，且必须填写原因。")
        approval = self.env["brew.approval.instance"].create_for_target(
            "material_overage", self, user, self.factory_id, metrics
        )
        self.write(
            {
                "state": "overage_pending",
                "overage_reason": reason,
                "approval_instance_id": approval.id,
            }
        )
        return approval

    def action_mark_recommended(self):
        self.ensure_one()
        if self.state not in {"draft", "overage_pending"}:
            raise ValidationError("当前领料单不能推荐批次。")
        if (
            self.overage_metrics()["quantity"] > 0
            and (
                not self.approval_instance_id
                or self.approval_instance_id.state != "approved"
            )
        ):
            raise ValidationError("超领审批通过后才能推荐批次。")
        for line in self.line_ids:
            allocated = sum(line.allocation_ids.mapped("recommended_qty"))
            if abs(allocated - line.requested_qty) > 1e-8:
                raise ValidationError(f"{line.product_id.display_name} 推荐批次数量不足。")
        self.write({"state": "recommended"})
        return True

    def action_issue(self):
        self.ensure_one()
        if self.state != "recommended":
            raise ValidationError("只有已推荐批次的领料单可以发料。")
        for line in self.line_ids:
            for allocation in line.allocation_ids:
                self._brew_create_done_move(
                    name=self.name,
                    product=line.product_id,
                    quantity=allocation.recommended_qty,
                    uom=line.uom_id,
                    source_location=allocation.location_id,
                    destination_location=self.production_location_id,
                    lot=allocation.lot_id,
                    source_model=self._name,
                    source_id=self.id,
                    source_reference=self.name,
                )
                allocation.write({"issued_qty": allocation.recommended_qty})
                reservation = self.env["brew.inventory.reservation"].sudo().search(
                    [
                        ("company_id", "=", self.company_id.id),
                        ("source_model", "=", self._name),
                        ("source_id", "=", self.id),
                        ("product_id", "=", line.product_id.id),
                        ("lot_id", "=", allocation.lot_id.id),
                        ("location_id", "=", allocation.location_id.id),
                        ("state", "=", "active"),
                    ],
                    limit=1,
                )
                if reservation:
                    reservation.write({"state": "consumed"})
        self.write({"state": "issued", "issued_at": fields.Datetime.now()})
        return True

    def action_acknowledge(self):
        self.ensure_one()
        if self.state != "issued":
            raise ValidationError("只有已发料领料单可以确认实领。")
        for allocation in self.line_ids.mapped("allocation_ids"):
            if (
                not math.isfinite(allocation.acknowledged_qty)
                or allocation.acknowledged_qty < 0
                or allocation.acknowledged_qty
                > allocation.issued_qty + 1e-8
            ):
                raise ValidationError("实领数量不能小于零或超过发料数量。")
        self.write({"state": "acknowledged", "acknowledged_at": fields.Datetime.now()})
        return True

    def action_return_materials(self, return_values):
        self.ensure_one()
        if self.state != "acknowledged":
            raise ValidationError("只有已确认实领的领料单可以退料。")
        allocations = {item.id: item for item in self.line_ids.mapped("allocation_ids")}
        for values in return_values:
            allocation = allocations.get(int(values.get("allocation_id") or 0))
            if not allocation:
                raise ValidationError("退料批次不属于当前领料单。")
            total = float(values.get("returned_qty") or 0)
            quantity = total - allocation.returned_qty
            if not math.isfinite(total) or quantity <= 0:
                raise ValidationError("累计退料数量必须大于当前已退数量。")
            available = allocation.acknowledged_qty - allocation.returned_qty
            if quantity > available + 1e-8:
                raise ValidationError("退料数量不能超过该批次可退数量。")
            self._brew_create_done_move(
                name=self.name,
                product=allocation.line_id.product_id,
                quantity=quantity,
                uom=allocation.line_id.uom_id,
                source_location=self.production_location_id,
                destination_location=allocation.location_id,
                lot=allocation.lot_id,
                source_model=self._name,
                source_id=self.id,
                source_reference=self.name,
            )
            allocation.write({"returned_qty": total})
        return True

    def action_complete(self):
        self.ensure_one()
        if self.state != "acknowledged":
            raise ValidationError("只有已确认实领的领料单可以完成。")
        for allocation in self.line_ids.mapped("allocation_ids"):
            if any(not math.isfinite(value) or value < 0 for value in (allocation.returned_qty, allocation.consumed_qty, allocation.loss_qty, allocation.acknowledged_qty)):
                raise ValidationError("退回、耗用与损耗必须是非负有效数量。")
            if abs(allocation.issued_qty - allocation.acknowledged_qty) > 1e-8:
                raise ValidationError("发料与实领数量不一致，请先处理差异。")
            if allocation.returned_qty > allocation.acknowledged_qty:
                raise ValidationError("退料数量不能超过实领数量。")
            accounted = (
                allocation.returned_qty
                + allocation.consumed_qty
                + allocation.loss_qty
            )
            if abs(accounted - allocation.acknowledged_qty) > 1e-8:
                raise ValidationError("每个实领批次的退回、耗用与损耗数量必须守恒。")
            product = allocation.line_id.product_id
            if allocation.consumed_qty:
                production_location = product.with_company(
                    self.company_id
                ).property_stock_production
                self._brew_create_done_move(
                    name=self.name,
                    product=product,
                    quantity=allocation.consumed_qty,
                    uom=allocation.line_id.uom_id,
                    source_location=self.production_location_id,
                    destination_location=production_location,
                    lot=allocation.lot_id,
                    source_model=self._name,
                    source_id=self.id,
                    source_reference=self.name,
                )
            if allocation.loss_qty:
                loss_location = product.with_company(
                    self.company_id
                ).property_stock_inventory
                self._brew_create_done_move(
                    name=self.name,
                    product=product,
                    quantity=allocation.loss_qty,
                    uom=allocation.line_id.uom_id,
                    source_location=self.production_location_id,
                    destination_location=loss_location,
                    lot=allocation.lot_id,
                    source_model=self._name,
                    source_id=self.id,
                    source_reference=self.name,
                )
        self.write({"state": "completed", "completed_at": fields.Datetime.now()})
        return True


class BrewMaterialRequisitionLine(models.Model):
    _name = "brew.material.requisition.line"
    _description = "Craft Brew Material Requisition Line"
    _check_company_auto = True

    requisition_id = fields.Many2one(
        "brew.material.requisition", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="requisition_id.company_id", store=True, index=True
    )
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    planned_qty = fields.Float(required=True)
    requested_qty = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    allocation_ids = fields.One2many(
        "brew.material.requisition.allocation", "line_id"
    )
    issued_qty = fields.Float(compute="_compute_totals", store=True)
    acknowledged_qty = fields.Float(compute="_compute_totals", store=True)
    returned_qty = fields.Float(compute="_compute_totals", store=True)
    consumed_qty = fields.Float(compute="_compute_totals", store=True)
    loss_qty = fields.Float(compute="_compute_totals", store=True)

    _sql_constraints = [
        ("planned_positive", "check(planned_qty > 0)", "计划用量必须大于零。"),
        ("requested_positive", "check(requested_qty > 0)", "申请用量必须大于零。"),
    ]

    @api.depends(
        "allocation_ids.issued_qty",
        "allocation_ids.acknowledged_qty",
        "allocation_ids.returned_qty",
        "allocation_ids.consumed_qty",
        "allocation_ids.loss_qty",
    )
    def _compute_totals(self):
        for line in self:
            line.issued_qty = sum(line.allocation_ids.mapped("issued_qty"))
            line.acknowledged_qty = sum(line.allocation_ids.mapped("acknowledged_qty"))
            line.returned_qty = sum(line.allocation_ids.mapped("returned_qty"))
            line.consumed_qty = sum(line.allocation_ids.mapped("consumed_qty"))
            line.loss_qty = sum(line.allocation_ids.mapped("loss_qty"))

    @api.constrains("product_id", "uom_id")
    def _check_uom(self):
        for line in self:
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("领料单位与物料单位类别不一致。")


class BrewMaterialRequisitionAllocation(models.Model):
    _name = "brew.material.requisition.allocation"
    _description = "Craft Brew Material Lot Allocation"
    _check_company_auto = True

    line_id = fields.Many2one(
        "brew.material.requisition.line", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="line_id.company_id", store=True, index=True
    )
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    location_id = fields.Many2one("stock.location", required=True, check_company=True)
    recommended_qty = fields.Float(required=True)
    issued_qty = fields.Float(default=0)
    acknowledged_qty = fields.Float(default=0)
    returned_qty = fields.Float(default=0)
    consumed_qty = fields.Float(default=0)
    loss_qty = fields.Float(default=0)

    _sql_constraints = [
        ("recommended_positive", "check(recommended_qty > 0)", "推荐数量必须大于零。"),
        ("issued_non_negative", "check(issued_qty >= 0)", "发料数量不能小于零。"),
        ("acknowledged_non_negative", "check(acknowledged_qty >= 0)", "实领数量不能小于零。"),
        ("returned_non_negative", "check(returned_qty >= 0)", "退料数量不能小于零。"),
        ("consumed_non_negative", "check(consumed_qty >= 0)", "耗用数量不能小于零。"),
        ("loss_non_negative", "check(loss_qty >= 0)", "损耗数量不能小于零。"),
    ]


class BrewTank(models.Model):
    _inherit = "brew.tank"

    current_production_batch_id = fields.Many2one(
        "brew.production.batch", string="当前生产批次", check_company=True, copy=False
    )


class BrewProductionPlan(models.Model):
    _inherit = "brew.production.plan"

    source_production_batch_id = fields.Many2one(
        "brew.production.batch",
        string="参考生产批次工艺",
        check_company=True,
        ondelete="restrict",
        index=True,
    )


class BrewProductionBatch(models.Model):
    _name = "brew.production.batch"
    _description = "Craft Brew Production Batch"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    product_code_snapshot = fields.Char(readonly=True, copy=False)
    finished_product_code = fields.Char(readonly=True, copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="production_plan_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="production_plan_id.factory_id", store=True, index=True
    )
    production_plan_id = fields.Many2one(
        "brew.production.plan", required=True, check_company=True, index=True
    )
    product_tmpl_id = fields.Many2one(
        "product.template",
        related="production_plan_id.product_tmpl_id",
        store=True,
        check_company=True,
    )
    recipe_version_id = fields.Many2one(
        "brew.recipe.version",
        related="production_plan_id.recipe_version_id",
        store=True,
        check_company=True,
    )
    tank_id = fields.Many2one(
        "brew.tank", related="production_plan_id.tank_id", store=True, check_company=True
    )
    planned_volume_l = fields.Float(
        related="production_plan_id.planned_volume_l", store=True
    )
    actual_volume_l = fields.Float(default=0)
    recipe_snapshot_json = fields.Text(required=True, default="{}")
    material_snapshot_json = fields.Text(required=True, default="{}")
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("in_progress", "生产中"),
            ("in_tank", "已进罐"),
            ("completed", "已完成"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    started_at = fields.Datetime(readonly=True)
    entered_tank_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    record_ids = fields.One2many("brew.production.record", "batch_id")
    material_actual_ids = fields.One2many("brew.production.material.actual", "batch_id")

    _sql_constraints = [
        ("plan_unique", "unique(production_plan_id)", "一个生产计划只能创建一个生产批次。"),
        ("finished_product_code_unique", "unique(company_id, finished_product_code)", "成品编号不能重复。"),
        ("actual_volume_non_negative", "check(actual_volume_l >= 0)", "实际体积不能小于零。"),
    ]

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            plan = self.env["brew.production.plan"].browse(
                values.get("production_plan_id")
            )
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.production.batch") or "New"
                )
            if plan:
                values.setdefault("recipe_snapshot_json", plan.recipe_snapshot_json or "{}")
                values.setdefault(
                    "material_snapshot_json", plan.material_snapshot_json or "{}"
                )
        batches = super().create(values_list)
        batches._assign_product_identifiers()
        return batches

    def _assign_product_identifiers(self, force=False):
        for batch in self:
            if batch.finished_product_code and not force:
                continue
            product = batch.product_tmpl_id
            code = (product.product_variant_id.default_code or "").strip() or f"BEER-{product.id}"
            super(BrewProductionBatch, batch).write({
                "product_code_snapshot": code,
                "finished_product_code": f"{code}-{batch.name}",
            })

    def init(self):
        # Backfill existing batches once. Later edits to a beer's code must not
        # alter identifiers already issued for production.
        self.env.cr.execute("""
            UPDATE brew_production_batch AS batch
               SET product_code_snapshot = COALESCE(NULLIF(TRIM(product.default_code), ''), 'BEER-' || plan.product_tmpl_id),
                   finished_product_code = COALESCE(NULLIF(TRIM(product.default_code), ''), 'BEER-' || plan.product_tmpl_id) || '-' || batch.name
              FROM brew_production_plan AS plan
              JOIN product_product AS product ON product.id = (
                  SELECT MIN(variant.id) FROM product_product AS variant
                   WHERE variant.product_tmpl_id = plan.product_tmpl_id)
             WHERE batch.production_plan_id = plan.id
               AND (batch.finished_product_code IS NULL OR batch.finished_product_code = '')
        """)

    def action_start(self):
        self.ensure_one()
        if self.state != "draft" or self.production_plan_id.state != "dispatched":
            raise ValidationError("只有已下发计划的草稿批次可以开始。")
        requisitions = self.env["brew.material.requisition"].sudo().search([("production_plan_id", "=", self.production_plan_id.id), ("state", "!=", "cancelled")])
        if not requisitions or any(record.state not in {"acknowledged", "completed"} for record in requisitions):
            raise ValidationError("请先由库管发料，并由生产负责人确认实领后再开始生产。")
        self.write({"state": "in_progress", "started_at": fields.Datetime.now()})
        self.production_plan_id.with_context(brew_state_transition=True).write(
            {"state": "in_progress"}
        )
        return True

    def action_enter_tank(self, actual_volume_l):
        self.ensure_one()
        volume = float(actual_volume_l or 0)
        cooling = self.record_ids.filtered(
            lambda record: record.record_type == "cooling" and record.state == "approved"
        )
        if self.state != "in_progress" or not cooling:
            raise ValidationError("冷却记录审核通过后才能确认进罐。")
        if volume <= 0 or volume > self.tank_id.capacity_l:
            raise ValidationError("实际进罐体积必须大于零且不能超过罐容。")
        if (
            self.tank_id.status != "empty"
            or self.tank_id.cleaning_status != "clean"
            or self.tank_id.current_production_batch_id
        ):
            raise ValidationError("目标罐必须为空罐且已清洗。")
        self.write(
            {
                "state": "in_tank",
                "actual_volume_l": volume,
                "entered_tank_at": fields.Datetime.now(),
            }
        )
        self.tank_id.write(
            {
                "status": "fermenting",
                "current_product": self.product_tmpl_id.display_name,
                "current_batch_no": self.name,
                "current_volume_l": volume,
                "current_production_batch_id": self.id,
            }
        )
        return True


class BrewProductionRecord(models.Model):
    _name = "brew.production.record"
    _description = "Craft Brew Production Record"
    _order = "recorded_at, id"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    batch_id = fields.Many2one(
        "brew.production.batch", required=True, ondelete="restrict", check_company=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="batch_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="batch_id.factory_id", store=True, index=True
    )
    record_type = fields.Selection(
        [
            ("cleaning", "清洗"),
            ("mashing", "糖化"),
            ("lautering", "过滤"),
            ("boiling", "煮沸"),
            ("cooling", "冷却"),
            ("yeast", "添加酵母"),
            ("other", "其他"),
        ],
        required=True,
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("submitted", "待审核"),
            ("approved", "已审核"),
            ("returned", "已退回"),
            ("superseded", "已更正"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    version = fields.Integer(required=True, default=1)
    corrects_record_id = fields.Many2one(
        "brew.production.record", check_company=True, index=True, ondelete="restrict"
    )
    correction_reason = fields.Text()
    equipment_id = fields.Many2one(
        "maintenance.equipment", check_company=True, index=True
    )
    operator_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, check_company=True
    )
    recorded_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    started_at = fields.Datetime()
    ended_at = fields.Datetime()
    parameters_json = fields.Text(required=True, default="{}")
    note = fields.Text()
    attachment_ids = fields.Many2many("ir.attachment", string="现场照片/附件")
    material_actual_ids = fields.One2many(
        "brew.production.material.actual", "production_record_id"
    )
    submitted_at = fields.Datetime(readonly=True)
    approved_by_id = fields.Many2one("res.users", check_company=True, readonly=True)
    approved_at = fields.Datetime(readonly=True)
    return_reason = fields.Text()

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.production.record") or "New"
                )
        return super().create(values_list)

    def write(self, values):
        protected = {
            "batch_id",
            "record_type",
            "equipment_id",
            "operator_id",
            "recorded_at",
            "started_at",
            "ended_at",
            "parameters_json",
            "note",
            "attachment_ids",
            "material_actual_ids",
        }
        if not self.env.context.get("brew_state_transition") and protected.intersection(values):
            if any(record.state not in {"draft", "returned"} for record in self):
                raise ValidationError("只有草稿或退回生产记录可以修改。")
        return super().write(values)

    @api.constrains("parameters_json")
    def _check_parameters_json(self):
        for record in self:
            try:
                value = json.loads(record.parameters_json or "{}")
            except (TypeError, ValueError) as exc:
                raise ValidationError("生产参数必须是有效 JSON。") from exc
            if not isinstance(value, dict):
                raise ValidationError("生产参数必须是 JSON 对象。")

    def action_submit(self):
        self.ensure_one()
        if self.state not in {"draft", "returned"}:
            raise ValidationError("当前生产记录不能提交。")
        if self.batch_id.state not in {"in_progress", "in_tank"}:
            raise ValidationError("生产批次尚未开始或已经结束。")
        self.with_context(brew_state_transition=True).write(
            {"state": "submitted", "submitted_at": fields.Datetime.now(), "return_reason": False}
        )
        return True

    def action_approve(self, user):
        self.ensure_one()
        if self.state != "submitted":
            raise ValidationError("只有待审核生产记录可以审核。")
        self.with_context(brew_state_transition=True).write(
            {
                "state": "approved",
                "approved_by_id": user.id,
                "approved_at": fields.Datetime.now(),
            }
        )
        if self.corrects_record_id:
            self.corrects_record_id.with_context(brew_state_transition=True).write(
                {"state": "superseded"}
            )
        return True

    def action_return(self, reason):
        self.ensure_one()
        if self.state != "submitted" or not str(reason or "").strip():
            raise ValidationError("只有待审核记录可退回，且必须填写原因。")
        self.with_context(brew_state_transition=True).write(
            {"state": "returned", "return_reason": reason}
        )
        return True

    def create_correction(self, reason):
        self.ensure_one()
        if self.state != "approved" or not str(reason or "").strip():
            raise ValidationError("只有已审核记录可以更正，且必须填写原因。")
        return self.copy(
            {
                "name": "New",
                "state": "draft",
                "version": self.version + 1,
                "corrects_record_id": self.id,
                "correction_reason": reason,
                "submitted_at": False,
                "approved_by_id": False,
                "approved_at": False,
            }
        )


class BrewProductionMaterialActual(models.Model):
    _name = "brew.production.material.actual"
    _description = "Craft Brew Actual Material Trace"
    _check_company_auto = True

    batch_id = fields.Many2one(
        "brew.production.batch", required=True, ondelete="cascade", check_company=True, index=True
    )
    production_record_id = fields.Many2one(
        "brew.production.record", ondelete="cascade", check_company=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="batch_id.company_id", store=True, index=True
    )
    product_id = fields.Many2one("product.product", required=True, check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, check_company=True)
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    action_type = fields.Selection(
        [("consumed", "耗用"), ("yeast", "添加酵母"), ("adjunct", "添加辅料")],
        required=True,
        default="consumed",
    )

    _sql_constraints = [
        ("quantity_positive", "check(quantity > 0)", "实际物料数量必须大于零。"),
    ]

    @api.constrains("product_id", "lot_id", "uom_id", "production_record_id", "batch_id")
    def _check_relations(self):
        for actual in self:
            if actual.lot_id.product_id != actual.product_id:
                raise ValidationError("实际物料批次与产品不一致。")
            if actual.uom_id.category_id != actual.product_id.uom_id.category_id:
                raise ValidationError("实际物料单位与产品单位类别不一致。")
            if actual.production_record_id and actual.production_record_id.batch_id != actual.batch_id:
                raise ValidationError("实际物料记录必须属于同一生产批次。")
