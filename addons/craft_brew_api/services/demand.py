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
        if not self.user.has_group("craft_brew_core.group_brew_production_director"):
            raise ApiError("PERMISSION_DENIED", "只有生产总监可以确认或退回需求。", 403)

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
        return {
            "id": str(demand.id),
            "name": demand.name,
            "factory_id": str(demand.factory_id.id) if demand.factory_id else None,
            "salesperson": {"id": str(demand.salesperson_id.id), "name": demand.salesperson_id.name},
            "customer": {"id": str(demand.customer_id.id), "name": demand.customer_id.display_name},
            "product": {"id": str(demand.product_tmpl_id.id), "name": demand.product_tmpl_id.display_name},
            "packaging": {"id": str(demand.packaging_id.id), "name": demand.packaging_id.display_name} if demand.packaging_id else None,
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
        if any(key in payload for key in {"customer_id", "product_tmpl_id", "packaging_id", "factory_id"}):
            merged = {"customer_id": demand.customer_id.id, "product_tmpl_id": demand.product_tmpl_id.id, "packaging_id": demand.packaging_id.id, "factory_id": demand.factory_id.id, **payload}
            customer, product, packaging, factory = self._relations(merged)
            values.update({"customer_id": customer.id, "product_tmpl_id": product.id, "packaging_id": packaging.id or False, "factory_id": factory.id or False})
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
        elif action == "confirm":
            self._ensure_director()
            demand.action_confirm(self.user)
        elif action == "return":
            self._ensure_director()
            demand.action_return(payload.get("reason"))
        elif action == "cancel":
            if demand.salesperson_id != self.user and not self._is_manager():
                raise ApiError("PERMISSION_DENIED", "只能取消本人创建的需求。", 403)
            demand.action_cancel(payload.get("reason"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "产品需求动作不存在。", 404)
        return demand, self.serialize(demand)
