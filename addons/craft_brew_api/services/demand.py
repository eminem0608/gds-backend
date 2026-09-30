import math

from odoo import fields

from ..controllers.base import ApiError


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
                "craft_brew_core.group_brew_admin",
            ]
        )

    def _ensure_sales(self):
        if not (self.user.has_group("craft_brew_core.group_brew_sales") or self.user.has_group("craft_brew_core.group_brew_admin")):
            raise ApiError("PERMISSION_DENIED", "只有销售可以维护产品需求。", 403)

    def _ensure_director(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有生产总监或系统管理员可以确认或退回需求。", 403)

    def _create_review_tasks(self, demand):
        task_model = self.env["brew.task"].sudo()
        for role_code in ("production_director", "tenant_admin"):
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
        if not self._is_manager():
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
            "customer": {"id": str(demand.customer_id.id), "name": demand.customer_id.display_name},
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
            "submitted_at": demand.submitted_at.isoformat() if demand.submitted_at else None,
            "confirmed_at": demand.confirmed_at.isoformat() if demand.confirmed_at else None,
            "version": demand.write_date.isoformat() if demand.write_date else None,
        }

    def list(self, page, page_size, state=None, keyword=""):
        model = self.env["brew.product.demand"].sudo()
        domain = self._domain()
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
        if not customer or not product:
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
        return customer, product, packaging, factory

    def _packaging_values(self, payload, packaging):
        method = payload.get("packaging_method")
        if method is None:
            return {}
        if method not in {"keg", "box", "other"}:
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
        if quantity <= 0 or not quantity.is_integer() or volume_l <= 0:
            raise ApiError("VALIDATION_RANGE", "包装数量须为正整数，所需酒液须大于零。", 422)
        if not units_per_box_raw.is_integer():
            raise ApiError("VALIDATION_RANGE", "每箱数量须为整数。", 422)
        units_per_box = int(units_per_box_raw)
        if method == "other":
            description = str(payload.get("packaging_description") or "").strip()
            if not description:
                raise ApiError("VALIDATION_REQUIRED", "请填写其他包装的说明。", 422)
            if packaging:
                raise ApiError("VALIDATION_FORMAT", "其他包装不应选择预设规格。", 422)
            return {"packaging_method": method, "package_container_type": False, "package_unit_volume_l": 0,
                    "package_units_per_box": 0, "packaging_description": description}
        if unit_l <= 0:
            raise ApiError("VALIDATION_RANGE", "请填写单桶或单瓶容量。", 422)
        if method == "box":
            container = payload.get("package_container_type")
            if container not in {"bottle", "can"} or units_per_box <= 0:
                raise ApiError("VALIDATION_REQUIRED", "箱装需要选择瓶/罐类型和每箱数量。", 422)
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
                "customer_id": customer.id,
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

    def update(self, demand_id, payload):
        self._ensure_sales()
        demand = self._record(demand_id)
        self._check_version(demand, payload.get("version"))
        values = {key: payload[key] for key in {"package_qty", "requested_volume_l", "requested_delivery_date", "customer_reference", "notes"} if key in payload}
        if any(key in payload for key in {"customer_id", "product_tmpl_id", "packaging_id", "factory_id", "packaging_method"}):
            merged = {"customer_id": demand.customer_id.id, "product_tmpl_id": demand.product_tmpl_id.id, "packaging_id": demand.packaging_id.id, "factory_id": demand.factory_id.id, **payload}
            customer, product, packaging, factory = self._relations(merged)
            values.update({"customer_id": customer.id, "product_tmpl_id": product.id, "packaging_id": packaging.id or False, "factory_id": factory.id or False})
            values.update(self._packaging_values({"package_qty": demand.package_qty, "requested_volume_l": demand.requested_volume_l, **merged}, packaging))
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        demand.write(values)
        return demand, self.serialize(demand)

    def action(self, demand_id, action, payload):
        demand = self._record(demand_id)
        self._check_version(demand, payload.get("version"))
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
