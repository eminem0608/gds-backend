import math

from odoo import fields

from ..controllers.base import ApiError
from .production_lifecycle import can_close, require_close, close_plan


class DemandService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company

    def _is_manager(self):
        return any(
            self.user.has_group(group)
            for group in [
                "craft_brew_core.group_brew_general_manager",
                "craft_brew_core.group_brew_production_director",
                "craft_brew_core.group_brew_headquarters_planner",
                "craft_brew_core.group_brew_admin",
            ]
        )

    def _ensure_sales(self):
        if not (self.user.has_group("craft_brew_core.group_brew_sales") or self.user.has_group("craft_brew_core.group_brew_headquarters_planner") or self.user.has_group("craft_brew_core.group_brew_admin")):
            raise ApiError("PERMISSION_DENIED", "只有销售或总部生产计划人员可以维护产品需求。", 403)

    def _ensure_director(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_headquarters_planner")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有总部生产计划、生产总监或系统管理员可以确认或退回需求。", 403)

    def _create_review_tasks(self, demand):
        task_model = self.env["brew.task"].sudo()
        for role_code in ("headquarters_planner", "production_director", "tenant_admin"):
            task_model.create({
                "name": f"产品需求确认 · {demand.product_tmpl_id.name}",
                "task_type": "demand_review",
                "company_id": self.company.id,
                "role_code": role_code,
                "target_model": demand._name,
                "target_id": demand.id,
            })

    def _complete_review_tasks(self, demand, comment):
        self.env["brew.task"].sudo().search([
            ("company_id", "=", self.company.id),
            ("task_type", "=", "demand_review"),
            ("target_model", "=", demand._name),
            ("target_id", "=", demand.id),
            ("state", "not in", ["done", "cancelled"]),
        ]).write({"state": "done", "completed_at": fields.Datetime.now(), "last_comment": comment})

    def _domain(self):
        domain = [("company_id", "=", self.company.id)]
        if not self.user.brew_has_headquarters_scope(self.company):
            domain += [("factory_id", "in", self.user.brew_allowed_factories(self.company).ids)]
        if not self._is_manager() and not self.user.has_group("craft_brew_core.group_brew_brewery_manager"):
            domain += [("salesperson_id", "=", self.user.id)]
        return domain

    def _record(self, demand_id):
        demand = self.env["brew.product.demand"].sudo().search(
            self._domain() + [("id", "=", int(demand_id))], limit=1
        )
        if not demand:
            raise ApiError("RESOURCE_NOT_FOUND", "产品需求不存在。", 404)
        return demand

    def _check_version(self, demand, expected):
        current = demand.write_date.isoformat() if demand.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})

    def serialize(self, demand):
        packaging = demand.packaging_id
        method = demand.packaging_method or (
            "keg" if packaging.brew_barcode_level == "keg" else
            "box" if packaging.brew_barcode_level == "box" else
            "other" if packaging else None
        )
        units_per_box = demand.package_units_per_box or (int(packaging.qty) if method == "box" and packaging else 0)
        unit_volume_l = demand.package_unit_volume_l or (
            packaging.brew_volume_l / units_per_box if method == "box" and units_per_box else
            packaging.brew_volume_l if method == "keg" and packaging else 0
        )
        return {
            "id": str(demand.id),
            "name": demand.name,
            "factory_id": str(demand.factory_id.id) if demand.factory_id else None,
            "salesperson": {"id": str(demand.salesperson_id.id), "name": demand.salesperson_id.name},
            "customer": {"id": str(demand.customer_id.id), "name": demand.customer_id.display_name} if demand.customer_id else None,
            "product": {"id": str(demand.product_tmpl_id.id), "name": demand.product_tmpl_id.name},
            "packaging": {"id": str(demand.packaging_id.id), "name": demand.packaging_id.name} if demand.packaging_id else None,
            "packaging_method": method,
            "package_container_type": demand.package_container_type or ("can" if method == "box" and packaging and "罐" in packaging.name else "bottle" if method == "box" and packaging and "瓶" in packaging.name else None),
            "package_unit_volume_l": unit_volume_l,
            "package_units_per_box": units_per_box,
            "packaging_description": demand.packaging_description or "",
            "package_qty": demand.package_qty,
            "requested_volume_l": demand.requested_volume_l,
            "requested_delivery_date": demand.requested_delivery_date.isoformat() if demand.requested_delivery_date else None,
            "customer_reference": demand.customer_reference,
            "notes": demand.notes,
            "state": demand.state,
            "decision_reason": demand.decision_reason,
            "can_close": can_close(self.user) and not bool(demand.deleted_at),
            "deleted_at": demand.deleted_at.isoformat() if demand.deleted_at else None,
            "closed_at": demand.closed_at.isoformat() if demand.closed_at else None,
            "closed_by": demand.closed_by_id.name or None,
            "submitted_at": demand.submitted_at.isoformat() if demand.submitted_at else None,
            "confirmed_at": demand.confirmed_at.isoformat() if demand.confirmed_at else None,
            "version": demand.write_date.isoformat() if demand.write_date else None,
        }

    def list(self, page, page_size, state=None, keyword=""):
        model = self.env["brew.product.demand"].sudo()
        domain = self._domain() + [("deleted_at", "=", False)]
        if state:
            domain += [("state", "=", state)]
        if keyword:
            domain += ["|", ("name", "ilike", keyword), ("customer_id.name", "ilike", keyword)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="requested_delivery_date,id")
        return [self.serialize(item) for item in records], total

    def _relations(self, payload):
        customer = self.env["res.partner"].sudo().search(
            [("id", "=", int(payload.get("customer_id") or 0)), ("company_id", "=", self.company.id), ("customer_rank", ">", 0), ("active", "=", True)], limit=1
        )
        product = self.env["product.template"].sudo().search(
            [("id", "=", int(payload.get("product_tmpl_id") or 0)), ("company_id", "=", self.company.id), ("brew_item_type", "=", "finished_product"), ("active", "=", True)], limit=1
        )
        if (payload.get("customer_id") and not customer) or not product:
            raise ApiError("DATA_SCOPE_DENIED", "客户或产品不属于当前企业或已停用。", 403)
        packaging_id = payload.get("packaging_id")
        packaging = self.env["product.packaging"].sudo().search(
            [("id", "=", int(packaging_id)), ("product_id.product_tmpl_id", "=", product.id), ("active", "=", True)], limit=1
        ) if packaging_id else self.env["product.packaging"]
        if packaging_id and not packaging:
            raise ApiError("VALIDATION_FORMAT", "包装规格不属于所选产品。", 422)
        factory_id = payload.get("factory_id")
        factory = self.env["brew.factory"].sudo().search(
            [("id", "=", int(factory_id)), ("company_id", "=", self.company.id), ("active", "=", True)], limit=1
        ) if factory_id else self.env["brew.factory"]
        if factory_id and not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区不属于当前企业或已停用。", 403)
        if not self.user.brew_has_headquarters_scope(self.company):
            allowed = self.user.brew_allowed_factories(self.company)
            factory = factory or allowed[:1]
            if not factory or factory not in allowed:
                raise ApiError("DATA_SCOPE_DENIED", "需求只能归属于本厂区。", 403)
        if factory_id and not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区不属于当前企业或已停用。", 403)
        return customer, product, packaging, factory

    def _packaging_values(self, payload, packaging):
        method = payload.get("packaging_method")
        if method not in {None, False, "", "keg", "box", "other"}:
            raise ApiError("VALIDATION_FORMAT", "包装方式无效。", 422)
        try:
            quantity = float(payload.get("package_qty") or 0)
            volume_l = float(payload.get("requested_volume_l") or 0)
            unit_l = float(payload.get("package_unit_volume_l") or 0)
            units_per_box_raw = float(payload.get("package_units_per_box") or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError("VALIDATION_FORMAT", "包装数量或容量格式无效。", 422) from exc
        if not all(math.isfinite(value) for value in (quantity, volume_l, unit_l, units_per_box_raw)):
            raise ApiError("VALIDATION_FORMAT", "包装数量或容量格式无效。", 422)
        if quantity < 0 or not quantity.is_integer() or volume_l <= 0:
            raise ApiError("VALIDATION_RANGE", "包装数量须为非负整数，所需酒液须大于零。", 422)
        if unit_l < 0 or units_per_box_raw < 0 or not units_per_box_raw.is_integer():
            raise ApiError("VALIDATION_RANGE", "包装容量不能为负，每箱数量须为非负整数。", 422)
        units_per_box = int(units_per_box_raw)
        if not method:
            if packaging or quantity or unit_l or units_per_box or payload.get("package_container_type") or payload.get("packaging_description"):
                raise ApiError("VALIDATION_REQUIRED", "填写包装要求时请先选择包装方式。", 422)
            return {"packaging_method": False, "package_container_type": False, "package_unit_volume_l": 0,
                    "package_units_per_box": 0, "packaging_description": False}
        if method == "other":
            description = str(payload.get("packaging_description") or "").strip()
            if packaging:
                raise ApiError("VALIDATION_FORMAT", "其他包装不应选择预设规格。", 422)
            return {"packaging_method": method, "package_container_type": False, "package_unit_volume_l": 0,
                    "package_units_per_box": 0, "packaging_description": description}
        if method == "box":
            container = payload.get("package_container_type") or False
            if container not in {False, "bottle", "can"}:
                raise ApiError("VALIDATION_FORMAT", "包装类型无效。", 422)
        else:
            container = False
            units_per_box = 0
        packaged_volume = quantity * unit_l * (units_per_box if method == "box" else 1)
        if volume_l + 0.0001 < packaged_volume:
            raise ApiError("VALIDATION_RANGE", "所需酒液不能少于包装成品的总容量。", 422)
        if packaging:
            expected_level = "keg" if method == "keg" else "box"
            spec_volume = unit_l * (units_per_box if method == "box" else 1)
            if packaging.brew_barcode_level != expected_level or abs(packaging.brew_volume_l - spec_volume) > 0.001:
                raise ApiError("VALIDATION_RELATION", "所选包装规格与包装方式或容量不一致。", 422)
        return {"packaging_method": method, "package_container_type": container,
                "package_unit_volume_l": unit_l, "package_units_per_box": units_per_box,
                "packaging_description": False}

    def create(self, payload):
        self._ensure_sales()
        customer, product, packaging, factory = self._relations(payload)
        demand = self.env["brew.product.demand"].sudo().create(
            {
                "company_id": self.company.id,
                "factory_id": factory.id or False,
                "salesperson_id": self.user.id,
                "customer_id": customer.id or False,
                "product_tmpl_id": product.id,
                "packaging_id": packaging.id or False,
                **self._packaging_values(payload, packaging),
                "package_qty": float(payload.get("package_qty") or 0),
                "requested_volume_l": float(payload.get("requested_volume_l") or 0),
                "requested_delivery_date": payload.get("requested_delivery_date"),
                "customer_reference": payload.get("customer_reference"),
                "notes": payload.get("notes"),
            }
        )
        return demand, self.serialize(demand)

    def create_customer(self, payload):
        self._ensure_sales()
        name = str(payload.get("name") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        if not name or len(name) > 100 or len(phone) > 100:
            raise ApiError("VALIDATION_REQUIRED", "请填写不超过 100 字的客户名称，电话也不能超过 100 字。", 422)
        if self.env["res.partner"].sudo().with_context(active_test=False).search_count([("company_id", "=", self.company.id), ("name", "=ilike", name)]):
            raise ApiError("VALIDATION_DUPLICATE", "当前企业已存在同名客户或供应商，请先选择已有客户。", 422)
        customer = self.env["res.partner"].sudo().create({"name": name, "phone": phone, "company_id": self.company.id, "customer_rank": 1})
        return customer, {"id": str(customer.id), "name": customer.name}

    def update(self, demand_id, payload):
        self._ensure_sales()
        demand = self._record(demand_id)
        self._check_version(demand, payload.get("version"))
        values = {key: payload[key] for key in {"package_qty", "requested_volume_l", "requested_delivery_date", "customer_reference", "notes"} if key in payload}
        if any(key in payload for key in {"customer_id", "product_tmpl_id", "packaging_id", "factory_id", "packaging_method", "package_container_type", "package_unit_volume_l", "package_units_per_box", "packaging_description", "package_qty", "requested_volume_l"}):
            merged = {"customer_id": demand.customer_id.id, "product_tmpl_id": demand.product_tmpl_id.id, "packaging_id": demand.packaging_id.id, "factory_id": demand.factory_id.id, "packaging_method": demand.packaging_method, "package_container_type": demand.package_container_type, "package_unit_volume_l": demand.package_unit_volume_l, "package_units_per_box": demand.package_units_per_box, "packaging_description": demand.packaging_description, "package_qty": demand.package_qty, "requested_volume_l": demand.requested_volume_l, **payload}
            customer, product, packaging, factory = self._relations(merged)
            values.update({"customer_id": customer.id, "product_tmpl_id": product.id, "packaging_id": packaging.id or False, "factory_id": factory.id or False})
            values.update(self._packaging_values(merged, packaging))
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        demand.write(values)
        return demand, self.serialize(demand)

    def action(self, demand_id, action, payload):
        demand = self._record(demand_id)
        self.env.cr.execute("SELECT id FROM brew_product_demand WHERE id = %s FOR UPDATE", [demand.id])
        demand.invalidate_recordset()
        self._check_version(demand, payload.get("version"))
        if demand.deleted_at:
            raise ApiError("STATE_PRECONDITION_FAILED", "产品需求已删除。", 409)
        if action in {"cancel", "delete"} and (can_close(self.user) or action == "delete" or demand.state == "planned"):
            require_close(self.user)
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                raise ApiError("VALIDATION_REQUIRED", "请填写取消或删除原因。", 422)
            if action == "cancel" and demand.state == "cancelled":
                raise ApiError("STATE_PRECONDITION_FAILED", "产品需求已取消。", 409)
            from .planning import PlanningService
            with self.env.cr.savepoint():
                plans = self.env['brew.production.plan'].sudo().search([
                    ('company_id', '=', self.company.id), ('demand_id', '=', demand.id),
                    ('state', 'not in', ['cancelled', 'terminated'])], order='id')
                service = PlanningService(self.env, self.user, self.company)
                for plan in plans:
                    service._record(plan.id)
                    self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
                    plan.invalidate_recordset()
                    if plan.state not in {'cancelled', 'terminated'}:
                        close_plan(self.env, plan, self.user, '需求关闭：' + reason)
                values = {'state':'cancelled', 'decision_reason':reason, 'closed_at':fields.Datetime.now(), 'closed_by_id':self.user.id}
                if action == 'delete':
                    values.update(deleted_at=fields.Datetime.now(), deleted_by_id=self.user.id)
                demand.with_context(brew_state_transition=True).write(values)
                self._complete_review_tasks(demand, "需求已取消：" + reason)
            return demand, self.serialize(demand)
        if action == "submit":
            self._ensure_sales()
            demand.action_submit()
            self._create_review_tasks(demand)
        elif action == "confirm":
            self._ensure_director()
            demand.action_confirm(self.user)
            self._complete_review_tasks(demand, "需求已确认")
        elif action == "return":
            self._ensure_director()
            demand.action_return(payload.get("reason"))
            self._complete_review_tasks(demand, f"退回修改：{demand.decision_reason}")
        elif action == "cancel":
            if demand.salesperson_id != self.user and not self._is_manager():
                raise ApiError("PERMISSION_DENIED", "只能取消本人创建的需求。", 403)
            demand.action_cancel(payload.get("reason"))
            self._complete_review_tasks(demand, "需求已取消")
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "产品需求动作不存在。", 404)
        return demand, self.serialize(demand)
