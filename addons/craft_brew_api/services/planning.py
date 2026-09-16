import json

from ..controllers.base import ApiError


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
            "recipe_version": {"id": str(plan.recipe_version_id.id), "name": plan.recipe_version_id.name},
            "planned_volume_l": plan.planned_volume_l,
            "planned_start": plan.planned_start.isoformat() if plan.planned_start else None,
            "planned_end": plan.planned_end.isoformat() if plan.planned_end else None,
            "equipment": {"id": str(plan.equipment_id.id), "name": plan.equipment_id.name},
            "tank": {"id": str(plan.tank_id.id), "name": plan.tank_id.name},
            "owner": {"id": str(plan.owner_id.id), "name": plan.owner_id.name} if plan.owner_id else None,
            "approval_instance_id": str(plan.approval_instance_id.id) if plan.approval_instance_id else None,
            "dispatched_at": plan.dispatched_at.isoformat() if plan.dispatched_at else None,
            "cancel_reason": plan.cancel_reason,
            "version": plan.write_date.isoformat() if plan.write_date else None,
        }
        if detail:
            data.update({"resource_check": json.loads(plan.resource_check_json or "{}"), "recipe_snapshot": json.loads(plan.recipe_snapshot_json or "{}"), "material_snapshot": json.loads(plan.material_snapshot_json or "[]")})
        return data

    def list(self, page, page_size, state=None):
        model = self.env["brew.production.plan"].sudo()
        domain = [("company_id", "=", self.company.id)]
        if state:
            domain += [("state", "=", state)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="planned_start,id")
        return [self.serialize(item) for item in records], total

    def _relations(self, payload):
        factory = self.env["brew.factory"].sudo().search([("id", "=", int(payload.get("factory_id") or 0)), ("company_id", "=", self.company.id), ("active", "=", True)], limit=1)
        version = self.env["brew.recipe.version"].sudo().search([("id", "=", int(payload.get("recipe_version_id") or 0)), ("company_id", "=", self.company.id), ("state", "=", "active")], limit=1)
        equipment = self.env["maintenance.equipment"].sudo().search([("id", "=", int(payload.get("equipment_id") or 0)), ("company_id", "=", self.company.id), ("brew_factory_id", "=", factory.id if factory else 0), ("active", "=", True)], limit=1)
        tank = self.env["brew.tank"].sudo().search([("id", "=", int(payload.get("tank_id") or 0)), ("company_id", "=", self.company.id), ("factory_id", "=", factory.id if factory else 0), ("active", "=", True)], limit=1)
        if not all([factory, version, equipment, tank]):
            raise ApiError("DATA_SCOPE_DENIED", "厂区、配方版本、设备或罐体无效。", 403)
        return factory, version, equipment, tank

    def create(self, payload):
        self._require_manage()
        factory, version, equipment, tank = self._relations(payload)
        demand = self.env["brew.product.demand"].sudo().search([("id", "=", int(payload.get("demand_id") or 0)), ("company_id", "=", self.company.id), ("state", "=", "confirmed")], limit=1) if payload.get("demand_id") else self.env["brew.product.demand"]
        if payload.get("demand_id") and not demand:
            raise ApiError("STATE_PRECONDITION_FAILED", "来源需求不存在或尚未确认。", 422)
        plan = self.env["brew.production.plan"].sudo().create(
            {"company_id": self.company.id, "factory_id": factory.id, "demand_id": demand.id or False, "product_tmpl_id": version.recipe_id.product_tmpl_id.id, "recipe_version_id": version.id, "planned_volume_l": float(payload.get("planned_volume_l") or 0), "planned_start": payload.get("planned_start"), "planned_end": payload.get("planned_end"), "equipment_id": equipment.id, "tank_id": tank.id, "owner_id": int(payload["owner_id"]) if payload.get("owner_id") else False}
        )
        return plan, self.serialize(plan, True)

    def update(self, plan_id, payload):
        self._require_manage()
        plan = self._record(plan_id)
        self._check_version(plan, payload.get("version"))
        allowed = {"planned_volume_l", "planned_start", "planned_end", "owner_id"}
        values = {key: payload[key] for key in allowed if key in payload}
        if any(key in payload for key in {"factory_id", "recipe_version_id", "equipment_id", "tank_id"}):
            merged = {"factory_id": plan.factory_id.id, "recipe_version_id": plan.recipe_version_id.id, "equipment_id": plan.equipment_id.id, "tank_id": plan.tank_id.id, **payload}
            factory, version, equipment, tank = self._relations(merged)
            values.update({"factory_id": factory.id, "recipe_version_id": version.id, "product_tmpl_id": version.recipe_id.product_tmpl_id.id, "equipment_id": equipment.id, "tank_id": tank.id})
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        plan.write(values)
        return plan, self.serialize(plan, True)

    def action(self, plan_id, action, payload):
        plan = self._record(plan_id)
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
        if action == "submit": plan.action_submit(self.user)
        elif action == "approve": plan.action_approve()
        elif action == "dispatch": plan.action_dispatch()
        elif action == "cancel": plan.action_cancel(payload.get("reason"))
        elif action == "terminate": plan.action_terminate(payload.get("reason"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "生产计划动作不存在。", 404)
        return plan, self.serialize(plan, True)

    def progress(self, plan_id):
        plan = self._record(plan_id)
        tasks = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id)])
        return {"plan": self.serialize(plan), "tasks": [{"id": str(task.id), "name": task.name, "type": task.task_type, "state": task.state, "assignee": task.assignee_id.name if task.assignee_id else None} for task in tasks], "approval": {"id": str(plan.approval_instance_id.id), "state": plan.approval_instance_id.state} if plan.approval_instance_id else None}
