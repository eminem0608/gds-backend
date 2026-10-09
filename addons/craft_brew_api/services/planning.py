import json
import math
from types import SimpleNamespace
from datetime import datetime, timezone

from odoo import Command, fields
from .inventory import InventoryService

from ..controllers.base import ApiError
from .recipe_design import design_values


class PlanningService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not any(
            user.has_group(group)
            for group in {
                "craft_brew_core.group_brew_general_manager",
                "craft_brew_core.group_brew_production_director",
                "craft_brew_core.group_brew_admin",
                "craft_brew_core.group_brew_operator",
                "craft_brew_core.group_brew_warehouse",
            }
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看生产计划。", 403)

    def _require_manage(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有生产总监或系统管理员可以维护生产计划。", 403)

    def _require_approve(self):
        if not any(
            self.user.has_group(group)
            for group in {
                "craft_brew_core.group_brew_general_manager",
                "craft_brew_core.group_brew_production_director",
                "craft_brew_core.group_brew_admin",
            }
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权审批生产计划。", 403)

    def _record(self, plan_id):
        plan = self.env["brew.production.plan"].sudo().search(
            [("id", "=", int(plan_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not plan:
            raise ApiError("RESOURCE_NOT_FOUND", "生产计划不存在。", 404)
        privileged = self.user.has_group("craft_brew_core.group_brew_admin") or self.user.has_group("craft_brew_core.group_brew_general_manager")
        if not privileged and plan.factory_id not in self.user.brew_factory_ids:
            raise ApiError("DATA_SCOPE_DENIED", "计划不在当前账号厂区范围内。", 403)
        if self.user.has_group("craft_brew_core.group_brew_operator") and not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director"}):
            if self.user not in (plan.mash_owner_id | plan.fermentation_owner_id):
                raise ApiError("PERMISSION_DENIED", "只能查看分配给自己的生产批次。", 403)
        return plan

    def _check_version(self, record, expected):
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})

    def serialize(self, plan, detail=False):
        data = {
            "id": str(plan.id), "name": plan.name, "state": plan.state,
            "factory": {"id": str(plan.factory_id.id), "name": plan.factory_id.name},
            "demand_id": str(plan.demand_id.id) if plan.demand_id else None,
            "product": {"id": str(plan.product_tmpl_id.id), "name": plan.product_tmpl_id.display_name},
            "recipe_version": {"id": str(plan.recipe_version_id.id), "name": plan.recipe_version_id.name, "version": plan.recipe_version_id.version, "version_label": f"生产计划版 · {plan.name}" if plan.production_design_json and plan.production_design_json != "{}" else f"基础工艺 V{plan.recipe_version_id.version:02d}"},
            "source_production_batch": {"id": str(plan.source_production_batch_id.id), "name": plan.source_production_batch_id.name} if plan.source_production_batch_id else None,
            "planned_volume_l": plan.planned_volume_l,
            "planned_start": plan.planned_start.isoformat() if plan.planned_start else None,
            "planned_end": plan.planned_end.isoformat() if plan.planned_end else None,
            "equipment": {"id": str(plan.equipment_id.id), "name": plan.equipment_id.name},
            "tank": {"id": str(plan.tank_id.id), "name": plan.tank_id.name},
            "owner": {"id": str(plan.owner_id.id), "name": plan.owner_id.name} if plan.owner_id else None,
            "mash_owner": {"id": str(plan.mash_owner_id.id), "name": plan.mash_owner_id.name} if plan.mash_owner_id else None,
            "fermentation_owner": {"id": str(plan.fermentation_owner_id.id), "name": plan.fermentation_owner_id.name} if plan.fermentation_owner_id else None,
            "recipe_version_type": "production" if plan.production_design_json and plan.production_design_json != "{}" else "base",
            "approval_instance_id": str(plan.approval_instance_id.id) if plan.approval_instance_id else None,
            "dispatched_at": plan.dispatched_at.isoformat() if plan.dispatched_at else None,
            "cancel_reason": plan.cancel_reason,
            "version": plan.write_date.isoformat() if plan.write_date else None,
        }
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
        data["batch"] = {"id": str(batch.id), "name": batch.name, "state": batch.state} if batch else None
        if detail:
            data["stocking_request"] = json.loads(plan.stocking_request_json or "{}")
            data.update({"resource_check": json.loads(plan.resource_check_json or "{}"), "recipe_snapshot": json.loads(plan.recipe_snapshot_json or "{}"), "material_snapshot": json.loads(plan.material_snapshot_json or "[]"), "production_design": json.loads(plan.production_design_json or "{}")})
        return data

    def list(self, page, page_size, state=None):
        model = self.env["brew.production.plan"].sudo()
        domain = [("company_id", "=", self.company.id)]
        if not (self.user.has_group("craft_brew_core.group_brew_admin") or self.user.has_group("craft_brew_core.group_brew_general_manager")):
            domain.append(("factory_id", "in", self.user.brew_factory_ids.ids))
        if not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director"}):
            domain += ["|", ("mash_owner_id", "=", self.user.id), ("fermentation_owner_id", "=", self.user.id)]
        if state:
            domain += [("state", "=", state)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="planned_start,id")
        return [self.serialize(item) for item in records], total

    def assignees(self, factory_id):
        self._require_manage()
        factory = self.env["brew.factory"].sudo().search(
            [("id", "=", int(factory_id or 0)), ("company_id", "=", self.company.id), ("active", "=", True)], limit=1
        )
        if not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区无效，无法加载生产负责人。", 403)
        eligible_group = "craft_brew_core.group_brew_operator"
        users = self.env["res.users"].sudo().search(
            [("company_ids", "in", [self.company.id]), ("brew_factory_ids", "in", [factory.id]), ("active", "=", True), ("share", "=", False)],
            order="name,id",
        )
        return [
            {"id": str(user.id), "name": user.name}
            for user in users
            if user.has_group(eligible_group)
        ]

    def _assignee(self, user_id, factory):
        if not user_id:
            return self.env["res.users"]
        user = self.env["res.users"].sudo().search(
            [("id", "=", int(user_id)), ("company_ids", "in", [self.company.id]), ("brew_factory_ids", "in", [factory.id]), ("active", "=", True), ("share", "=", False)],
            limit=1,
        )
        if not user or not user.has_group("craft_brew_core.group_brew_operator"):
            raise ApiError("DATA_SCOPE_DENIED", "负责人必须是该厂区有权限的操作人员。", 403)
        return user

    def _production_recipe_snapshot(self, plan, design):
        if not design:
            return None
        if not isinstance(design, dict) or design.get("schema_version") != 2:
            raise ApiError("VALIDATION_FORMAT", "生产版配方设计格式无效，请重新编辑后保存。", 422)
        version = plan.recipe_version_id
        recipe = version.recipe_id
        if int(design.get("brew_equipment_id") or 0) != recipe.equipment_id.id:
            raise ApiError("VALIDATION_RELATION", "生产版配方必须沿用基础工艺绑定的酿造系统。", 422)
        if abs(float(design.get("brew_volume_l") or 0) - plan.planned_volume_l) > 0.001:
            raise ApiError("VALIDATION_RELATION", "生产版设计产量必须与计划产量一致。", 422)
        normalized = design_values(self.env, self.company, design, plan.planned_volume_l)
        materials = []
        estimated_cost = 0.0
        for command in normalized.get("material_line_ids", []):
            if command[0] != 0:
                continue
            values = command[2]
            product = self.env["product.product"].sudo().browse(values["product_id"])
            uom = self.env["uom.uom"].sudo().browse(values["uom_id"])
            unit_cost = product.standard_price or 0.0
            cost_qty = uom._compute_quantity(values["quantity"], product.uom_id, round=False)
            line_cost = cost_qty * unit_cost
            estimated_cost += line_cost
            materials.append({
                "material_id": str(product.id), "material_name": product.display_name,
                "quantity": values["quantity"], "uom_id": str(uom.id), "uom_name": uom.name,
                "estimated_unit_cost": unit_cost, "estimated_cost": line_cost,
                "stage": values.get("stage"), "notes": values.get("notes"),
            })
        process_steps = []
        for command in normalized.get("process_step_ids", []):
            if command[0] != 0:
                continue
            values = command[2]
            process_steps.append({
                "sequence": values["sequence"], "name": values["name"], "phase": values["phase"],
                "action_type": values["action_type"],
                "mandatory_confirmation": values["mandatory_confirmation"],
                "target_temperature": values.get("target_temperature", 0),
                "target_duration_hours": values.get("target_duration_hours", 0),
                "config": json.loads(values.get("config_json") or "{}"),
            })
        source_batch = plan.source_production_batch_id
        snapshot = {
            "recipe_id": recipe.id, "recipe_code": recipe.code,
            "version_id": version.id, "version": version.version,
            "version_type": "production", "version_label": f"生产计划版 · {plan.name}",
            "production_version_id": plan.name,
            "source_version_id": version.id, "source_version": version.version,
            "source_version_label": f"基础工艺 V{version.version:02d}",
            "source_batch_id": source_batch.id if source_batch else None,
            "source_batch_name": source_batch.name if source_batch else None,
            "base_volume_l": plan.planned_volume_l, "design": design, "process_steps": process_steps,
        }
        calculation = {"volume_l": plan.planned_volume_l, "materials": materials, "estimated_cost": estimated_cost, "currency": version.currency_id.name}
        return snapshot, calculation

    def _scale_production_design(self, design, volume):
        result = json.loads(json.dumps(design or {}))
        old_volume = float(result.get("brew_volume_l") or volume or 0)
        volume = float(volume or 0)
        if old_volume <= 0 or volume <= 0:
            raise ApiError("VALIDATION_FORMAT", "生产版配方设计产量无效。", 422)
        factor = volume / old_volume
        for key in ("fermentables", "hops", "yeast"):
            for line in result.get(key) or []:
                line["quantity"] = round(float(line.get("quantity") or 0) * factor, 4)
        result["brew_volume_l"] = volume
        mash = result.setdefault("mash", {})
        mash["sparge_mode"] = "auto"
        grain = sum(float(line.get("quantity") or 0) for line in result.get("fermentables") or [])
        boil_time = float(mash.get("boil_time_min") or 0)
        pre_volume = volume * (1 + float(mash.get("evaporation_rate") or 0) / 100 * boil_time / 60)
        first = max(0, grain * (float(mash.get("water_grain_ratio") or 0) - float(mash.get("absorption_rate") or 0)) - float(mash.get("screen_water_l") or 0))
        sparge = max(0, pre_volume - first)
        count = max(1, int(float(mash.get("sparge_count") or 1)))
        each = math.floor(sparge * 100 / count) / 100
        mash["sparge_water_liters"] = [round(sparge - each * (count - 1), 2) if index == count - 1 else each for index in range(count)]
        return result

    def _relations(self, payload):
        factory = self.env["brew.factory"].sudo().search([("id", "=", int(payload.get("factory_id") or 0)), ("company_id", "=", self.company.id), ("active", "=", True)], limit=1)
        source_batch = self.env["brew.production.batch"]
        source_batch_id = int(payload.get("source_production_batch_id") or 0)
        if source_batch_id:
            source_batch = self.env["brew.production.batch"].sudo().search(
                [("id", "=", source_batch_id), ("company_id", "=", self.company.id), ("state", "in", ["in_tank", "completed"])],
                limit=1,
            )
            if not source_batch:
                raise ApiError("DATA_SCOPE_DENIED", "参考生产批次不存在，或尚未形成可复用的工艺记录。", 403)
            if not (self.user.has_group("craft_brew_core.group_brew_general_manager") or self.user.has_group("craft_brew_core.group_brew_admin")) and source_batch.factory_id not in self.user.brew_factory_ids:
                raise ApiError("DATA_SCOPE_DENIED", "参考生产批次不在当前账号厂区范围内。", 403)
            version_id = int(payload.get("recipe_version_id") or source_batch.recipe_version_id.id)
            version_domain = [("id", "=", version_id), ("company_id", "=", self.company.id)]
        else:
            version_id = int(payload.get("recipe_version_id") or 0)
            version_domain = [("id", "=", version_id), ("company_id", "=", self.company.id), ("state", "=", "active")]
        version = self.env["brew.recipe.version"].sudo().search(version_domain, limit=1)
        equipment = self.env["maintenance.equipment"].sudo().search([("id", "=", int(payload.get("equipment_id") or 0)), ("company_id", "=", self.company.id), ("brew_factory_id", "=", factory.id if factory else 0), ("active", "=", True)], limit=1)
        tank = self.env["brew.tank"].sudo().search([("id", "=", int(payload.get("tank_id") or 0)), ("company_id", "=", self.company.id), ("factory_id", "=", factory.id if factory else 0), ("active", "=", True)], limit=1)
        if not all([factory, version, equipment, tank]):
            raise ApiError("DATA_SCOPE_DENIED", "厂区、配方版本、设备或罐体无效。", 403)
        if not version.recipe_id.equipment_id:
            raise ApiError("VALIDATION_RELATION", "所选配方尚未绑定酿造系统，无法创建生产计划。", 422)
        if version.recipe_id.equipment_id != equipment:
            raise ApiError("VALIDATION_RELATION", "生产计划必须使用所选配方绑定的酿造系统。", 422)
        if source_batch and source_batch.recipe_version_id != version:
            raise ApiError("VALIDATION_RELATION", "参考批次必须沿用它实际使用的基础工艺版本。", 422)
        return factory, version, equipment, tank, source_batch

    def _plan_datetime(self, value, label):
        if not value:
            raise ApiError("VALIDATION_REQUIRED", f"{label}不能为空。", 422)
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            raise ApiError("VALIDATION_FORMAT", f"{label}格式无效，请重新选择日期和时间。", 422)

    def create(self, payload):
        self._require_manage()
        factory, version, equipment, tank, source_batch = self._relations(payload)
        owner = self._assignee(payload.get("owner_id"), factory)
        mash_owner = self._assignee(payload.get("mash_owner_id"), factory)
        fermentation_owner = self._assignee(payload.get("fermentation_owner_id"), factory)
        if not mash_owner or not fermentation_owner:
            raise ApiError("VALIDATION_REQUIRED", "请分别指定糖化负责人和发酵负责人。", 422)
        demand = self.env["brew.product.demand"].sudo().search([("id", "=", int(payload.get("demand_id") or 0)), ("company_id", "=", self.company.id), ("state", "=", "confirmed")], limit=1) if payload.get("demand_id") else self.env["brew.product.demand"]
        if payload.get("demand_id") and not demand:
            raise ApiError("STATE_PRECONDITION_FAILED", "来源需求不存在或尚未确认。", 422)
        if demand and demand.product_tmpl_id != version.recipe_id.product_tmpl_id:
            raise ApiError("VALIDATION_RELATION", "来源需求的产品与所选配方不一致。", 422)
        if demand and demand.factory_id and demand.factory_id != factory:
            raise ApiError("VALIDATION_RELATION", "来源需求的厂区与生产计划厂区不一致。", 422)
        if demand and self.env["brew.production.plan"].sudo().search_count([
            ("company_id", "=", self.company.id), ("demand_id", "=", demand.id),
            ("state", "not in", ["cancelled", "terminated"]),
        ]):
            raise ApiError("VALIDATION_DUPLICATE", "该需求已有生产计划，请勿重复创建。", 422)
        plan = self.env["brew.production.plan"].sudo().create(
            {"company_id": self.company.id, "factory_id": factory.id, "demand_id": demand.id or False, "product_tmpl_id": version.recipe_id.product_tmpl_id.id, "recipe_version_id": version.id, "source_production_batch_id": source_batch.id or False, "planned_volume_l": float(payload.get("planned_volume_l") or 0), "planned_start": self._plan_datetime(payload.get("planned_start"), "计划开始时间"), "planned_end": self._plan_datetime(payload.get("planned_end"), "预计结束时间"), "equipment_id": equipment.id, "tank_id": tank.id, "owner_id": owner.id or False, "mash_owner_id": mash_owner.id, "fermentation_owner_id": fermentation_owner.id, "production_design_json": json.dumps(payload.get("production_design") or {}, ensure_ascii=False)}
        )
        if payload.get("production_design"):
            snapshot, calculation = self._production_recipe_snapshot(plan, payload["production_design"])
            plan.with_context(brew_state_transition=True).write({"recipe_snapshot_json": json.dumps(snapshot, ensure_ascii=False), "material_snapshot_json": json.dumps(calculation, ensure_ascii=False)})
        if not payload.get("production_design"):
            plan.expand_materials()
        self.env["brew.production.batch"].sudo().create({"production_plan_id": plan.id})
        plan.ensure_execution_tasks()
        if payload.get("submit_stock_request"):
            self.request_stock(plan)
        return plan, self.serialize(plan, True)

    def update(self, plan_id, payload):
        self._require_manage()
        plan = self._record(plan_id)
        self._check_version(plan, payload.get("version"))
        allowed = {"planned_volume_l", "planned_start", "planned_end", "owner_id", "mash_owner_id", "fermentation_owner_id"}
        values = {key: payload[key] for key in allowed if key in payload}
        for key, label in [("planned_start", "计划开始时间"), ("planned_end", "预计结束时间")]:
            if key in values: values[key] = self._plan_datetime(values[key], label)
        if any(key in payload for key in {"factory_id", "recipe_version_id", "source_production_batch_id", "equipment_id", "tank_id"}):
            merged = {"factory_id": plan.factory_id.id, "recipe_version_id": plan.recipe_version_id.id, "source_production_batch_id": plan.source_production_batch_id.id or False, "equipment_id": plan.equipment_id.id, "tank_id": plan.tank_id.id, **payload}
            factory, version, equipment, tank, source_batch = self._relations(merged)
            values.update({"factory_id": factory.id, "recipe_version_id": version.id, "product_tmpl_id": version.recipe_id.product_tmpl_id.id, "source_production_batch_id": source_batch.id or False, "equipment_id": equipment.id, "tank_id": tank.id})
            values.update({"recipe_snapshot_json": "{}", "material_snapshot_json": "[]"})
            values["production_design_json"] = "{}"
        else:
            factory = plan.factory_id
        if "owner_id" in payload:
            values["owner_id"] = self._assignee(payload.get("owner_id"), factory).id or False
        for key in ("mash_owner_id", "fermentation_owner_id"):
            if key in payload:
                values[key] = self._assignee(payload.get(key), factory).id or False
        if "planned_volume_l" in payload:
            design = json.loads(plan.production_design_json or "{}")
            if design:
                design = self._scale_production_design(design, payload["planned_volume_l"])
                values["production_design_json"] = json.dumps(design, ensure_ascii=False)
            values.update({"recipe_snapshot_json": "{}", "material_snapshot_json": "[]"})
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        plan.write(values)
        design = json.loads(plan.production_design_json or "{}")
        if design:
            snapshot, calculation = self._production_recipe_snapshot(plan, design)
            plan.with_context(brew_state_transition=True).write({"recipe_snapshot_json": json.dumps(snapshot, ensure_ascii=False), "material_snapshot_json": json.dumps(calculation, ensure_ascii=False)})
        return plan, self.serialize(plan, True)

    def action(self, plan_id, action, payload):
        plan = self._record(plan_id)
        self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
        plan.invalidate_recordset()
        self._check_version(plan, payload.get("version"))
        if action == "approve":
            self._require_approve()
        else:
            self._require_manage()
        if action == "check-resources":
            result = plan.check_resources()
            return plan, {**self.serialize(plan, True), "check_result": result}
        if action == "expand-materials":
            result = plan.expand_materials()
            return plan, {**self.serialize(plan, True), "calculation": result}
        if action == "request-stock": self.request_stock(plan)
        elif action == "complete-stock":
            if plan.state != "stocking":
                raise ApiError("STATE_PRECONDITION_FAILED", "当前计划不在备货阶段。", 422)
            inventory = InventoryService(self.env, self.user, self.company)
            result = inventory.plan_inventory(plan.id)
            if not result["ok"]:
                raise ApiError("INVENTORY_INSUFFICIENT", "原料仍然不足，请先完成采购入库。", 409, result)
            request = json.loads(plan.stocking_request_json or "{}")
            request["completed_at"] = fields.Datetime.now().isoformat()
            plan.with_context(brew_state_transition=True).write({"state": "draft", "stocking_request_json": json.dumps(request, ensure_ascii=False)})
        elif action in {"submit", "dispatch"}:
            self._dispatch_directly(plan)
        elif action == "approve": plan.action_approve()
        elif action == "cancel": plan.action_cancel(payload.get("reason"))
        elif action == "terminate": plan.action_terminate(payload.get("reason"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "生产计划动作不存在。", 404)
        return plan, self.serialize(plan, True)

    def _dispatch_directly(self, plan):
        # Validate and reserve in the same transaction as dispatch. Legacy
        # pending approvals are retired only after all checks succeed.
        self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
        plan.invalidate_recordset()
        if plan.state not in {"draft", "checked", "pending_approval", "approved"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有未下发且已完成备货的计划可以下发。", 422)
        if not plan.mash_owner_id or not plan.fermentation_owner_id:
            raise ApiError("VALIDATION_REQUIRED", "请先指定糖化和发酵负责人。", 422)
        if plan.state in {"pending_approval", "approved"}:
            plan.with_context(brew_state_transition=True).write({"state": "draft"})
        result = plan.check_resources()
        if not result["ok"]:
            names = "、".join(item["name"] for item in result["conflicts"])
            raise ApiError("RESOURCE_CONFLICT", f"生产安排存在冲突：{names}。请调整生产时间、设备或罐体后重新下发。", 409, result)
        InventoryService(self.env, self.user, self.company).plan_inventory(plan.id, reserve=True)
        approval = plan.approval_instance_id
        if approval and approval.state not in {"cancelled", "returned"}:
            approval.write({"state": "cancelled", "completed_at": fields.Datetime.now()})
        plan.with_context(brew_state_transition=True).write({"state": "approved", "approval_instance_id": False})
        plan.action_dispatch()

    def request_stock(self, plan):
        if plan.state not in {"draft", "checked"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有未下发计划可以提交备货需求。", 422)
        inventory = InventoryService(self.env, self.user, self.company)
        result = inventory.plan_inventory(plan.id)
        shortages = [item for item in result["materials"] if item["shortage"] > 0]
        if not shortages:
            raise ApiError("STATE_PRECONDITION_FAILED", "原料库存充足，无需提交备货需求。", 422)
        lines = []
        for item in shortages:
            product = self.env["product.product"].sudo().browse(int(item["product"]["id"]))
            lines.append(Command.create({"product_id": product.id, "requested_qty": item["shortage"], "uom_id": product.uom_id.id, "allocation_ids": [Command.create({"production_plan_id": plan.id, "required_qty": item["required"], "shortage_qty": item["shortage"], "uom_id": product.uom_id.id})]}))
        requisition = self.env["brew.purchase.requisition"].sudo().create({"company_id": self.company.id, "factory_id": plan.factory_id.id, "need_date": plan.planned_start.date(), "requester_id": self.user.id, "request_type": "planned", "reason": f"{plan.name} 原料备货", "line_ids": lines})
        requisition.action_submit(self.user)
        request = {"requisition_id": str(requisition.id), "name": requisition.name, "submitted_at": fields.Datetime.now().isoformat(), "submitted_by": self.user.name, "materials": shortages}
        plan.with_context(brew_state_transition=True).write({"state": "stocking", "stocking_request_json": json.dumps(request, ensure_ascii=False)})

    def preview_inventory(self, payload):
        self._require_manage()
        inventory = InventoryService(self.env, self.user, self.company)
        factory = inventory._factory(payload.get("factory_id"))
        version = self.env["brew.recipe.version"].sudo().search([("id", "=", int(payload.get("recipe_version_id") or 0)), ("company_id", "=", self.company.id)], limit=1)
        source = self.env["brew.production.batch"].sudo().search([("id", "=", int(payload.get("source_production_batch_id") or 0)), ("company_id", "=", self.company.id), ("state", "in", ["in_tank", "completed"])], limit=1)
        if not version or version.recipe_id.equipment_id.brew_factory_id != factory or (source and (source.factory_id != factory or source.recipe_version_id != version)):
            raise ApiError("VALIDATION_RELATION", "配方或历史批次与厂区不一致。", 422)
        if payload.get("source_production_batch_id") and not source:
            raise ApiError("RESOURCE_NOT_FOUND", "历史批次不存在。", 404)
        volume = float(payload.get("planned_volume_l") or 0)
        if not math.isfinite(volume) or volume <= 0:
            raise ApiError("VALIDATION_RANGE", "请填写有效计划产量。", 422)
        design = payload.get("production_design")
        if design:
            plan = SimpleNamespace(recipe_version_id=version, source_production_batch_id=source, planned_volume_l=volume, name="库存预览")
            _, calculation = self._production_recipe_snapshot(plan, self._scale_production_design(design, volume))
        elif source:
            calculation = json.loads(source.material_snapshot_json or "{}")
            ratio = volume / source.planned_volume_l
            calculation["materials"] = [{**line, "quantity": float(line.get("quantity") or 0) * ratio} for line in calculation.get("materials", [])]
        else:
            calculation = version.calculate(volume)
        result = inventory.material_availability(calculation.get("materials", []), factory)
        demand_inventory = None
        if payload.get("demand_id"):
            demand = self.env["brew.product.demand"].sudo().search([("id", "=", int(payload["demand_id"])), ("company_id", "=", self.company.id), ("state", "=", "confirmed")], limit=1)
            if not demand or demand.product_tmpl_id != version.recipe_id.product_tmpl_id or (demand.factory_id and demand.factory_id != factory):
                raise ApiError("VALIDATION_RELATION", "来源需求与产品或厂区不一致。", 422)
            product = demand.product_tmpl_id.product_variant_id
            candidates = inventory._candidate_stock(product, factory) if product else []
            available_l = 0.0
            unknown = not bool(product)
            for candidate in candidates:
                order = self.env["brew.packaging.order"].sudo().search([("company_id", "=", self.company.id), ("finished_lot_id", "=", candidate["lot"].id), ("state", "=", "released")], limit=1)
                if order and order.output_qty > 0:
                    if not demand.packaging_id or order.packaging_id == demand.packaging_id:
                        available_l += candidate["quantity"] * order.output_volume_l / order.output_qty
                else:
                    unknown = True
            demand_inventory = {"product": demand.product_tmpl_id.name, "required_l": demand.requested_volume_l, "available_l": available_l, "shortage_l": max(demand.requested_volume_l - available_l, 0), "unknown_packaging": unknown, "delivery_date": demand.requested_delivery_date.isoformat() if demand.requested_delivery_date else None}
        return {"ok": all(item["shortage"] <= 0 for item in result), "materials": result, "demand_inventory": demand_inventory}

    def progress(self, plan_id):
        from .workflow import WorkflowService
        plan = self._record(plan_id)
        tasks = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id)], order="id")
        workflow = WorkflowService(self.env, self.user, self.company)
        timeline = [{"at": plan.create_date.isoformat(), "title": "创建生产计划", "by": plan.create_uid.name}]
        request = json.loads(plan.stocking_request_json or "{}")
        for key, title in [("submitted_at", "提交原料备货需求"), ("completed_at", "原料备货完成")]:
            if request.get(key): timeline.append({"at": request[key], "title": title, "by": request.get("submitted_by", "")})
        if plan.dispatched_at: timeline.append({"at": plan.dispatched_at.isoformat(), "title": "生产计划已下发", "by": ""})
        for task in tasks:
            for key, title in [("claimed_at", "领取任务"), ("started_at", "开始执行"), ("completed_at", "完成任务")]:
                at = task[key]
                if at: timeline.append({"at": at.isoformat(), "title": f"{task.name} · {title}", "by": task.assignee_id.name or ""})
            for node in task.mash_execution_checkpoints():
                if node.get("completed_at"): timeline.append({"at": node["completed_at"], "title": node.get("title"), "by": node.get("completed_by"), "value": node.get("value")})
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
        if batch:
            records = self.env["brew.production.record"].sudo().search([("batch_id", "=", batch.id)], order="recorded_at,id")
            for record in records:
                timeline.append({"at": record.recorded_at.isoformat(), "title": record.name, "by": record.operator_id.name, "state": record.state})
        timeline.sort(key=lambda item: item["at"])
        return {"plan": self.serialize(plan, True), "tasks": [workflow.serialize_task(task) for task in tasks], "timeline": timeline, "can_manage": self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin"), "approval": {"id": str(plan.approval_instance_id.id), "state": plan.approval_instance_id.state} if plan.approval_instance_id else None}
