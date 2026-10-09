import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class BrewProductionPlan(models.Model):
    _name = "brew.production.plan"
    _description = "Craft Brew Production Plan"
    _order = "planned_start, id"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    factory_id = fields.Many2one("brew.factory", required=True, check_company=True, index=True)
    demand_id = fields.Many2one("brew.product.demand", check_company=True, index=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, check_company=True, index=True)
    recipe_version_id = fields.Many2one("brew.recipe.version", required=True, check_company=True, index=True)
    planned_volume_l = fields.Float(required=True)
    planned_start = fields.Datetime(required=True, index=True)
    planned_end = fields.Datetime(required=True, index=True)
    equipment_id = fields.Many2one("maintenance.equipment", required=True, check_company=True, index=True)
    tank_id = fields.Many2one("brew.tank", required=True, check_company=True, index=True)
    owner_id = fields.Many2one("res.users", check_company=True, index=True)
    mash_owner_id = fields.Many2one("res.users", check_company=True, index=True)
    fermentation_owner_id = fields.Many2one("res.users", check_company=True, index=True)
    state = fields.Selection(
        [("draft", "草稿"), ("checked", "已校验"), ("stocking", "备货中"), ("pending_approval", "待审批"), ("approved", "已批准"), ("dispatched", "已下发"), ("in_progress", "执行中"), ("done", "已完成"), ("cancelled", "已取消"), ("terminated", "已终止")],
        required=True, default="draft", index=True,
    )
    recipe_snapshot_json = fields.Text(default="{}", readonly=True)
    material_snapshot_json = fields.Text(default="[]", readonly=True)
    production_design_json = fields.Text(default="{}", readonly=True)
    resource_check_json = fields.Text(default="{}", readonly=True)
    stocking_request_json = fields.Text(default="{}", readonly=True)
    approval_instance_id = fields.Many2one("brew.approval.instance", check_company=True)
    dispatched_at = fields.Datetime()
    cancel_reason = fields.Text()

    _sql_constraints = [
        ("company_name_unique", "unique(company_id, name)", "生产计划号必须唯一。"),
        ("volume_positive", "check(planned_volume_l > 0)", "计划体积必须大于零。"),
        ("time_order", "check(planned_end > planned_start)", "计划结束时间必须晚于开始时间。"),
    ]

    _editable_fields = {"factory_id", "demand_id", "product_tmpl_id", "recipe_version_id", "source_production_batch_id", "planned_volume_l", "planned_start", "planned_end", "equipment_id", "tank_id", "owner_id", "mash_owner_id", "fermentation_owner_id", "production_design_json"}

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.production.plan") or "New"
        return super().create(values_list)

    def write(self, values):
        if not self.env.context.get("brew_state_transition") and self._editable_fields.intersection(values):
            if any(plan.state not in {"draft", "checked"} for plan in self):
                raise ValidationError("只有草稿或已校验计划可以修改。")
            if self._editable_fields.intersection(values):
                values = {**values, "state": "draft", "resource_check_json": "{}"}
        return super().write(values)

    @api.constrains("factory_id", "equipment_id", "tank_id", "recipe_version_id", "source_production_batch_id", "product_tmpl_id", "planned_volume_l")
    def _check_relations(self):
        for plan in self:
            if plan.equipment_id.brew_factory_id != plan.factory_id or plan.tank_id.factory_id != plan.factory_id:
                raise ValidationError("设备和罐体必须属于计划厂区。")
            if plan.tank_id.capacity_l < plan.planned_volume_l:
                raise ValidationError("计划体积不能超过目标罐容量。")
            if plan.source_production_batch_id:
                source = plan.source_production_batch_id
                if source.state not in {"in_tank", "completed"}:
                    raise ValidationError("只能引用已进罐或已完成的生产批次工艺。")
                if source.product_tmpl_id != plan.product_tmpl_id or source.recipe_version_id != plan.recipe_version_id:
                    raise ValidationError("参考生产批次必须与计划产品及来源基础版本一致。")
            elif plan.recipe_version_id.state != "active":
                raise ValidationError("生产计划只能引用使用中的配方版本。")
            if plan.recipe_version_id.recipe_id.product_tmpl_id != plan.product_tmpl_id:
                raise ValidationError("配方版本与计划产品不一致。")

    def check_resources(self):
        self.ensure_one()
        if self.state not in {"draft", "checked"}:
            raise ValidationError("只有草稿或已校验计划可以重新校验资源。")
        conflicts = []
        plan_domain = [
            ("id", "!=", self.id),
            ("state", "not in", ["cancelled", "terminated", "done"]),
            ("planned_start", "<", self.planned_end),
            ("planned_end", ">", self.planned_start),
        ]
        for other in self.search(plan_domain + ["|", ("equipment_id", "=", self.equipment_id.id), ("tank_id", "=", self.tank_id.id)]):
            conflicts.append({"type": "production_plan", "id": str(other.id), "name": other.name,
                              "reason": "、".join(label for matches, label in [(other.equipment_id == self.equipment_id, "酿造设备占用"), (other.tank_id == self.tank_id, "罐体占用")] if matches),
                              "start_at": other.planned_start.isoformat(), "end_at": other.planned_end.isoformat()})
        unavailable = self.env["brew.equipment.unavailability"].search(
            [("equipment_id", "=", self.equipment_id.id), ("state", "=", "active"), ("start_at", "<", self.planned_end), ("end_at", ">", self.planned_start)]
        )
        for period in unavailable:
            conflicts.append({"type": "equipment_unavailability", "id": str(period.id), "name": period.name, "reason": "设备维修或停用", "start_at": period.start_at.isoformat(), "end_at": period.end_at.isoformat()})
        result = {"ok": not conflicts, "conflicts": conflicts, "checked_at": fields.Datetime.now().isoformat()}
        self.with_context(brew_state_transition=True).write({"resource_check_json": json.dumps(result, ensure_ascii=False), "state": "checked" if not conflicts else "draft"})
        return result

    def expand_materials(self):
        self.ensure_one()
        source_batch = self.source_production_batch_id
        if source_batch:
            recipe_snapshot = json.loads(source_batch.recipe_snapshot_json or "{}")
            recipe_snapshot.update({
                "version_type": "production",
                "version_label": f"生产批次版 · {source_batch.name}",
                "source_batch_id": source_batch.id,
                "source_batch_name": source_batch.name,
                "source_version_id": self.recipe_version_id.id,
                "source_version": self.recipe_version_id.version,
                "source_version_label": f"基础工艺 V{self.recipe_version_id.version:02d}",
            })
            base_calculation = json.loads(source_batch.material_snapshot_json or "{}")
            ratio = self.planned_volume_l / source_batch.planned_volume_l if source_batch.planned_volume_l else 1
            calculation = dict(base_calculation)
            calculation["volume_l"] = self.planned_volume_l
            calculation["materials"] = [
                {**line, "quantity": (line.get("quantity") or 0) * ratio, "estimated_cost": (line.get("estimated_cost") or 0) * ratio}
                for line in base_calculation.get("materials", [])
            ]
            calculation["estimated_cost"] = (base_calculation.get("estimated_cost") or 0) * ratio
        else:
            calculation = self.recipe_version_id.calculate(self.planned_volume_l)
            recipe_snapshot = {
                "recipe_id": self.recipe_version_id.recipe_id.id,
                "recipe_code": self.recipe_version_id.recipe_id.code,
                "version_id": self.recipe_version_id.id,
                "version": self.recipe_version_id.version,
                "version_type": "base",
                "version_label": f"基础工艺 V{self.recipe_version_id.version:02d}",
                "base_volume_l": self.recipe_version_id.base_volume_l,
                "design": json.loads(self.recipe_version_id.design_json or "{}"),
                "process_steps": [
                    {"sequence": step.sequence, "name": step.name, "phase": step.phase, "action_type": step.action_type, "mandatory_confirmation": step.mandatory_confirmation, "config": json.loads(step.config_json or "{}")}
                    for step in self.recipe_version_id.process_step_ids.sorted("sequence")
                ],
            }
        self.with_context(brew_state_transition=True).write(
            {"recipe_snapshot_json": json.dumps(recipe_snapshot, ensure_ascii=False), "material_snapshot_json": json.dumps(calculation, ensure_ascii=False)}
        )
        return calculation

    def action_submit(self, user):
        self.ensure_one()
        if self.state != "checked" or not json.loads(self.resource_check_json or "{}").get("ok"):
            raise ValidationError("计划必须先通过资源冲突校验。")
        if not self.material_snapshot_json or self.material_snapshot_json == "[]":
            self.expand_materials()
        approval = self.env["brew.approval.instance"].create_for_target(
            "production_plan", self, user, self.factory_id, {"quantity": self.planned_volume_l, "amount": json.loads(self.material_snapshot_json).get("estimated_cost", 0)}
        )
        self.with_context(brew_state_transition=True).write({"state": "pending_approval", "approval_instance_id": approval.id})
        return approval

    def action_approve(self):
        self.ensure_one()
        if self.state != "pending_approval" or self.approval_instance_id.state != "approved":
            raise ValidationError("审批实例尚未全部通过。")
        self.with_context(brew_state_transition=True).write({"state": "approved"})
        return True

    def action_dispatch(self):
        self.ensure_one()
        if self.state != "approved":
            raise ValidationError("只有已批准计划可以下发。")
        if not self.mash_owner_id or not self.fermentation_owner_id:
            raise ValidationError("下发生产计划前必须指定糖化负责人和发酵负责人。")
        self.ensure_execution_tasks()
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", self.id)], limit=1)
        if batch:
            batch.write({"recipe_snapshot_json": self.recipe_snapshot_json, "material_snapshot_json": self.material_snapshot_json})
        self.with_context(brew_state_transition=True).write({"state": "dispatched", "dispatched_at": fields.Datetime.now()})
        if self.demand_id:
            self.demand_id.action_mark_planned()
        return True

    def ensure_execution_tasks(self):
        self.ensure_one()
        tasks = self.env["brew.task"].sudo()
        for task_type, label, role, assignee in [
            ("mashing", "糖化执行", "operator", self.mash_owner_id.id),
            ("fermentation", "发酵执行", "operator", self.fermentation_owner_id.id),
            ("material_preparation", "备料", "warehouse_keeper", False),
        ]:
            existing = tasks.search([("target_model", "=", self._name), ("target_id", "=", self.id), ("task_type", "=", task_type), ("state", "!=", "cancelled")], limit=1)
            if not existing:
                existing = tasks.create({"name": f"{self.name} {label}", "task_type": task_type, "role_code": role, "assignee_id": assignee, "company_id": self.company_id.id, "factory_id": self.factory_id.id, "target_model": self._name, "target_id": self.id, "due_at": self.planned_start})
            if task_type == "mashing":
                existing.mash_execution_checkpoints()
        return True

    def action_cancel(self, reason):
        self.ensure_one()
        if self.state in {"in_progress", "done", "cancelled", "terminated"} or not str(reason or "").strip():
            raise ValidationError("当前计划不能取消，或未填写原因。")
        self.env["brew.task"].search([("target_model", "=", self._name), ("target_id", "=", self.id), ("state", "not in", ["done", "cancelled"])]).write({"state": "cancelled"})
        self.with_context(brew_state_transition=True).write({"state": "cancelled", "cancel_reason": reason})
        return True

    def action_terminate(self, reason):
        self.ensure_one()
        if self.state not in {"dispatched", "in_progress"} or not str(reason or "").strip():
            raise ValidationError("只有已下发或执行中计划可以终止，且必须填写原因。")
        self.env["brew.task"].search([("target_model", "=", self._name), ("target_id", "=", self.id), ("state", "not in", ["done", "cancelled"])]).write({"state": "cancelled"})
        self.with_context(brew_state_transition=True).write({"state": "terminated", "cancel_reason": reason})
        return True
