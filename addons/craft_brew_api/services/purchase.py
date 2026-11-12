from datetime import datetime, timezone
from math import isfinite

from odoo import Command, fields
from odoo.tools.float_utils import float_is_zero

from ..controllers.base import ApiError
from .inventory import InventoryService


PURCHASE_READ_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_brewery_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_purchase",
    "craft_brew_core.group_brew_finance",
    "craft_brew_core.group_brew_admin",
}


class PurchaseService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not any(user.has_group(group) for group in PURCHASE_READ_GROUPS):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看采购和收货。", 403)

    def _require_purchase(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_purchase")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有采购或系统管理员可以执行该操作。", 403)

    def _require_requisition_requester(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有酒厂生产总监或系统管理员可以发起采购需求。", 403)

    def _require_warehouse(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_warehouse")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有库管或系统管理员可以执行该操作。", 403)

    def _require_director(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_general_manager")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权审批采购单据。", 403)

    def _factory_ids(self):
        return self.user.brew_allowed_factories(self.company).ids

    def _factory(self, factory_id):
        factory = self.env["brew.factory"].sudo().search(
            [
                ("id", "=", int(factory_id or 0)),
                ("company_id", "=", self.company.id),
                ("id", "in", self._factory_ids()),
            ],
            limit=1,
        )
        if not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区不存在或不在当前账号范围内。", 403)
        return factory

    def _record(self, model, record_id, factory_field="factory_id"):
        record = self.env[model].sudo().search(
            [("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id)],
            limit=1,
        )
        factory = record[factory_field] if record and factory_field else None
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "采购业务单据不存在。", 404)
        if factory and factory.id not in self._factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "采购业务单据不在当前账号范围内。", 403)
        return record

    @staticmethod
    def _version(record):
        return record.write_date.isoformat() if record.write_date else None

    @staticmethod
    def _datetime_value(value, fallback=None):
        if not value:
            return fallback
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise ApiError("VALIDATION_FORMAT", "预计到货时间格式不正确。", 422) from exc
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    def _check_version(self, record, expected):
        current = self._version(record)
        if not expected or expected != current:
            raise ApiError(
                "VERSION_CONFLICT",
                "数据版本不一致，请刷新后重试。",
                409,
                {"current_version": current},
            )

    def _pending_approval_step(self, instance):
        if not instance or instance.state != 'pending':
            raise ApiError('STATE_PRECONDITION_FAILED', 'No pending approval step for this document.', 409)
        step = instance.step_ids.filtered(
            lambda item: item.state == 'pending'
            and item.sequence == instance.current_sequence
        )[:1]
        if not step:
            raise ApiError('STATE_PRECONDITION_FAILED', 'No pending approval step for this document.', 409)
        if step.role_code not in self.user.brew_role_codes() and not self.user.has_group("craft_brew_core.group_brew_admin"):
            raise ApiError('PERMISSION_DENIED', 'You cannot process this approval step.', 403)
        return step

    def _product(self, product_id):
        product = self.env["product.product"].sudo().search(
            [
                ("id", "=", int(product_id or 0)),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not product:
            raise ApiError("RESOURCE_NOT_FOUND", "采购物料不存在。", 404)
        if product.brew_item_type == "packaging_material" and product.brew_packaging_catalog_owner_id:
            raise ApiError("DATA_SCOPE_DENIED", "该商品属于供应商自己的目录，不是酒厂材料。", 403)
        if product.brew_item_type == "packaging_material" and product.brew_packaging_state and product.brew_packaging_state != "confirmed":
            raise ApiError("STATE_PRECONDITION_FAILED", "该包装商品尚未经过酒厂确认，不能采购。", 422)
        return product

    def _uom(self, uom_id, product):
        uom = self.env["uom.uom"].sudo().browse(int(uom_id or 0)).exists()
        if not uom or uom.category_id != product.uom_id.category_id:
            raise ApiError("VALIDATION_RELATION", "采购单位与物料不一致。", 422)
        return uom

    def _serialize_requisition(self, record):
        linked_orders = self.env["purchase.order"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("brew_requisition_ids", "in", [record.id]),
            ],
            order="date_order,id",
        )
        lines = []
        quoted_total = 0.0
        unpriced_line_count = 0
        for line in record.line_ids:
            product = line.product_id.product_tmpl_id
            unit_price = None
            line_amount = None
            if product.brew_supplier_price and product.uom_id.category_id == line.uom_id.category_id:
                unit_price = product.uom_id._compute_price(product.brew_supplier_price, line.uom_id)
                line_amount = unit_price * line.requested_qty
                quoted_total += line_amount
            else:
                unpriced_line_count += 1
            lines.append({
                "id": str(line.id),
                "product": {"id": str(line.product_id.id), "name": line.product_id.display_name},
                "requested_qty": line.requested_qty,
                "approved_qty": line.approved_qty,
                "ordered_qty": line.ordered_qty,
                "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                "estimated_unit_price": unit_price,
                "estimated_amount": line_amount,
                "allocations": [
                    {
                        "plan_id": str(allocation.production_plan_id.id),
                        "plan_name": allocation.production_plan_id.name,
                        "product_name": allocation.production_plan_id.product_tmpl_id.name,
                        "demand_name": allocation.production_plan_id.demand_id.name if allocation.production_plan_id.demand_id else None,
                        "required_qty": allocation.required_qty,
                        "shortage_qty": allocation.shortage_qty,
                        "source_shortage_qty": allocation.source_shortage_qty or allocation.shortage_qty,
                    }
                    for allocation in line.allocation_ids
                ],
            })
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "request_type": record.request_type,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "need_date": record.need_date.isoformat(),
            "reason": record.reason,
            "emergency_reason": record.emergency_reason,
            "preferred_supplier": (
                {
                    "id": str(record.preferred_supplier_id.id),
                    "name": record.preferred_supplier_id.name,
                    "code": record.preferred_supplier_id.brew_supplier_code,
                }
                if record.preferred_supplier_id
                else None
            ),
            "requester": {
                "id": str(record.requester_id.id),
                "name": record.requester_id.name,
            },
            "approval_instance_id": (
                str(record.approval_instance_id.id)
                if record.approval_instance_id
                else None
            ),
            "return_reason": record.return_reason,
            "approval_history": self._approval_history(record._name, record.id),
            "orders": [
                {
                    "id": str(order.id),
                    "name": order.name,
                    "state": order.state,
                    "approval_state": order.brew_approval_state,
                    "receipt_state": self._order_receipt_state(order),
                    "supplier": {
                        "id": str(order.partner_id.id),
                        "name": order.partner_id.name,
                    },
                }
                for order in linked_orders
            ],
            "lines": lines,
            "estimated_total_amount": quoted_total if not unpriced_line_count else None,
            "estimated_quoted_subtotal": quoted_total,
            "estimated_unpriced_line_count": unpriced_line_count,
            "version": self._version(record),
        }

    def requisitions(self, page, page_size, filters=None):
        filters = filters or {}
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if filters.get("keyword"):
            domain.extend(
                [
                    "|",
                    ("name", "ilike", filters["keyword"]),
                    ("line_ids.product_id", "ilike", filters["keyword"]),
                ]
            )
        if filters.get("requester_name"):
            domain.append(("requester_id.name", "ilike", filters["requester_name"]))
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("request_type"):
            domain.append(("request_type", "=", filters["request_type"]))
        if filters.get("state"):
            states = [item for item in str(filters["state"]).split(",") if item]
            if len(states) == 1:
                domain.append(("state", "=", states[0]))
            elif states:
                domain.append(("state", "in", states))
        if filters.get("date_from"):
            domain.append(("need_date", ">=", filters["date_from"]))
        if filters.get("date_to"):
            domain.append(("need_date", "<=", filters["date_to"]))
        model = self.env["brew.purchase.requisition"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="id desc",
        )
        return [self._serialize_requisition(item) for item in records], total

    def _committed_shortages(self, plan_ids):
        allocations = self.env["brew.purchase.requisition.allocation"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("production_plan_id", "in", plan_ids),
                ("requisition_line_id.requisition_id.state", "not in", ["cancelled", "closed"]),
            ]
        )
        committed = {}
        for allocation in allocations:
            key = (allocation.production_plan_id.id, allocation.requisition_line_id.product_id.id)
            committed[key] = committed.get(key, 0.0) + allocation.shortage_qty
        return committed

    def _linked_shortage_requisitions(self, plan_ids):
        allocations = self.env["brew.purchase.requisition.allocation"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("production_plan_id", "in", plan_ids),
            ],
            order="id desc",
        )
        linked = {}
        for allocation in allocations:
            line = allocation.requisition_line_id
            requisition = line.requisition_id
            key = (allocation.production_plan_id.id, line.product_id.id)
            existing = next(
                (item for item in linked.setdefault(key, []) if item["id"] == str(requisition.id)),
                None,
            )
            if existing:
                existing["quantity"] += allocation.shortage_qty
            else:
                linked[key].append(
                    {
                        "id": str(requisition.id),
                        "name": requisition.name,
                        "state": requisition.state,
                        "quantity": allocation.shortage_qty,
                    }
                )
        return linked

    def requisition_source_plans(self, filters=None):
        filters = filters or {}
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
            # InventoryService can recalculate shortages only before dispatch.
            ("state", "in", ["checked", "stocking", "pending_approval", "approved"]),
        ]
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("plan_no"):
            domain.extend([
                "|", "|",
                ("name", "ilike", filters["plan_no"]),
                ("demand_id.name", "ilike", filters["plan_no"]),
                ("product_tmpl_id.name", "ilike", filters["plan_no"]),
            ])
        # A plan belongs in the result whenever its production window overlaps
        # the selected window, including plans that start before the filter.
        if filters.get("date_from"):
            domain.append(("planned_end", ">=", f"{filters['date_from']} 00:00:00"))
        if filters.get("date_to"):
            domain.append(("planned_start", "<=", f"{filters['date_to']} 23:59:59"))
        plans = self.env["brew.production.plan"].sudo().search(
            domain,
            order="planned_start,id",
            limit=300,
        )
        inventory = InventoryService(self.env, self.user, self.company)
        committed = self._committed_shortages(plans.ids)
        linked_requisitions = self._linked_shortage_requisitions(plans.ids)
        category_codes = set()
        raw_results = []
        for plan in plans:
            shortage = inventory.plan_inventory(plan.id, reserve=False)
            raw_results.append((plan, shortage))
            for item in shortage["materials"]:
                if item["shortage"] > 0:
                    product = self.env["product.product"].sudo().browse(int(item["product"]["id"]))
                    if product.product_tmpl_id.brew_material_category:
                        category_codes.add(product.product_tmpl_id.brew_material_category)
        categories = self.env["brew.material.category"].sudo().search(
            [("code", "in", list(category_codes)), ("active", "=", True)]
        )
        category_by_code = {item.code: item for item in categories}
        result = []
        for plan, shortage in raw_results:
            materials = []
            for item in shortage["materials"]:
                if item["shortage"] <= 0:
                    continue
                product = self.env["product.product"].sudo().browse(int(item["product"]["id"]))
                template = product.product_tmpl_id
                category = category_by_code.get(template.brew_material_category)
                category_group = category.material_group if category else template.brew_material_category
                if filters.get("category_group") and category_group != filters["category_group"]:
                    continue
                if filters.get("category_code") and template.brew_material_category != filters["category_code"]:
                    continue
                requested_qty = committed.get((plan.id, product.id), 0.0)
                requestable_qty = max(item["shortage"] - requested_qty, 0.0)
                request_status = (
                    "unrequested" if requested_qty <= 0
                    else "partial" if not float_is_zero(
                        requestable_qty, precision_rounding=product.uom_id.rounding
                    )
                    else "requested"
                )
                materials.append(
                    {
                        **item,
                        "product": {
                            **item["product"],
                            "code": template.default_code,
                            "brand": template.brew_brand,
                            "specification": template.brew_specification,
                        },
                        "category": {
                            "code": template.brew_material_category,
                            "name": category.name if category else template.brew_material_category,
                            "group": category_group,
                            "group_name": dict(template._brew_material_category_selection()).get(category_group, category_group),
                        },
                        "requested_qty": requested_qty,
                        "linked_requisitions": linked_requisitions.get(
                            (plan.id, product.id), []
                        ),
                        "requestable_qty": requestable_qty,
                        "request_status": request_status,
                        "request_key": f"{plan.id}:{product.id}",
                    }
                )
            if not materials:
                continue
            result.append(
                {
                    "id": str(plan.id),
                    "name": plan.name,
                    "factory": {
                        "id": str(plan.factory_id.id),
                        "name": plan.factory_id.name,
                    },
                    "planned_start": (
                        plan.planned_start.isoformat() if plan.planned_start else None
                    ),
                    "planned_end": (
                        plan.planned_end.isoformat() if plan.planned_end else None
                    ),
                    "product": {
                        "id": str(plan.product_tmpl_id.id),
                        "name": plan.product_tmpl_id.display_name,
                    },
                    "demand": {
                        "id": str(plan.demand_id.id),
                        "name": plan.demand_id.name,
                    } if plan.demand_id else None,
                    "materials": materials,
                    "shortage_items": sum(1 for item in materials if item["requestable_qty"] > 0),
                }
            )
        return result

    def create_requisition(self, payload):
        self._require_requisition_requester()
        if payload.get("request_type") != "emergency":
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "普通采购需求必须由生产计划缺口汇总生成。",
                422,
            )
        emergency_reason = str(payload.get("emergency_reason") or "").strip()
        if not emergency_reason:
            raise ApiError("VALIDATION_REQUIRED", "紧急采购必须填写紧急原因。", 422)
        factory = self._factory(payload.get("factory_id"))
        lines = payload.get("lines") or []
        if not payload.get("need_date") or not lines:
            raise ApiError("VALIDATION_REQUIRED", "需求日期和采购明细不能为空。", 422)
        commands = []
        for values in lines:
            product = self._product(values.get("product_id"))
            uom = self._uom(values.get("uom_id"), product)
            quantity = float(values.get("requested_qty") or 0)
            if quantity <= 0:
                raise ApiError("VALIDATION_RANGE", "采购需求数量必须大于零。", 422)
            commands.append(
                Command.create(
                    {
                        "product_id": product.id,
                        "requested_qty": quantity,
                        "uom_id": uom.id,
                    }
                )
            )
        record = self.env["brew.purchase.requisition"].sudo().create(
            {
                "company_id": self.company.id,
                "factory_id": factory.id,
                "need_date": payload["need_date"],
                "request_type": "emergency",
                "reason": payload.get("reason"),
                "emergency_reason": emergency_reason,
                "preferred_supplier_id": (
                    self._supplier(payload.get("supplier_id")).id
                    if payload.get("supplier_id")
                    else False
                ),
                "requester_id": self.user.id,
                "line_ids": commands,
            }
        )
        # Create and submit in the same API transaction; submission errors roll back creation.
        record.action_submit(self.user)
        return record, self._serialize_requisition(record)

    def update_requisition(self, record_id, payload):
        self._require_requisition_requester()
        record = self._record("brew.purchase.requisition", record_id)
        self._check_version(record, payload.get("version"))
        if record.state not in {"draft", "returned"}:
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "只有草稿或已退回的采购需求可以修改。",
                422,
            )
        if any(line.ordered_qty for line in record.line_ids):
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "已关联采购订单的需求明细不能修改。",
                422,
            )

        values = {}
        if "factory_id" in payload:
            factory = self._factory(payload.get("factory_id"))
            if record.line_ids.mapped("allocation_ids") and factory != record.factory_id:
                raise ApiError(
                    "VALIDATION_RELATION",
                    "生产缺口生成的采购需求不能更换厂区。",
                    422,
                )
            values["factory_id"] = factory.id
        if "need_date" in payload:
            if not payload.get("need_date"):
                raise ApiError("VALIDATION_REQUIRED", "需求日期不能为空。", 422)
            values["need_date"] = payload["need_date"]
        if "reason" in payload:
            values["reason"] = payload.get("reason") or False
        if "emergency_reason" in payload:
            emergency_reason = str(payload.get("emergency_reason") or "").strip()
            if record.request_type == "emergency" and not emergency_reason:
                raise ApiError("VALIDATION_REQUIRED", "紧急采购必须填写紧急原因。", 422)
            values["emergency_reason"] = emergency_reason or False
        if "supplier_id" in payload:
            values["preferred_supplier_id"] = (
                self._supplier(payload.get("supplier_id")).id
                if payload.get("supplier_id")
                else False
            )

        line_payloads = payload.get("lines") if "lines" in payload else None
        if line_payloads is not None:
            if not line_payloads:
                raise ApiError("VALIDATION_REQUIRED", "采购需求明细不能为空。", 422)
            existing = record.line_ids
            existing_by_id = {line.id: line for line in existing}
            retained_ids = set()
            product_ids = set()
            commands = []
            for item in line_payloads:
                line_id = int(item.get("id") or 0)
                if line_id:
                    if line_id in retained_ids or line_id not in existing_by_id:
                        raise ApiError("VALIDATION_RELATION", "采购需求明细与当前单据不一致。", 422)
                    retained_ids.add(line_id)
                product = self._product(item.get("product_id"))
                uom = self._uom(item.get("uom_id") or product.uom_id.id, product)
                quantity = float(item.get("requested_qty") or 0)
                if quantity <= 0:
                    raise ApiError("VALIDATION_RANGE", "采购需求数量必须大于零。", 422)
                if product.id in product_ids:
                    raise ApiError("VALIDATION_RELATION", "同一种原料请合并为一条采购需求明细。", 422)
                product_ids.add(product.id)
                line_values = {
                    "product_id": product.id,
                    "requested_qty": quantity,
                    "approved_qty": 0,
                    "uom_id": uom.id,
                }
                if not line_id:
                    commands.append(Command.create(line_values))
                    continue
                line = existing_by_id[line_id]
                allocations = line.allocation_ids
                if allocations and line.product_id != product:
                    line_values["allocation_ids"] = [Command.clear()]
                elif allocations and not float_is_zero(
                    quantity - line.requested_qty,
                    precision_rounding=line.uom_id.rounding,
                ):
                    allocated = sum(allocations.mapped("shortage_qty"))
                    if allocated:
                        ratio = quantity / allocated
                        line_values["allocation_ids"] = [
                            Command.update(allocation.id, {"shortage_qty": allocation.shortage_qty * ratio})
                            for allocation in allocations
                        ]
                commands.append(Command.update(line_id, line_values))
            commands.extend(
                Command.delete(line.id) for line in existing if line.id not in retained_ids
            )
            values["line_ids"] = commands

        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的采购需求字段。", 422)
        record.write(values)
        if payload.get("submit_after_save") is True:
            record.action_submit(self.user)
        return record, self._serialize_requisition(record)

    def aggregate_requisitions(self, payload):
        self._require_requisition_requester()
        selected_items = {}
        for item in payload.get("items") or []:
            key = (int(item.get("plan_id") or 0), int(item.get("product_id") or 0))
            if not all(key) or key in selected_items:
                raise ApiError("VALIDATION_RELATION", "采购原料来源重复或无效。", 422)
            selected_items[key] = item.get("requested_qty")
        plan_ids = list(dict.fromkeys(
            [int(value) for value in payload.get("plan_ids") or []]
            + [item[0] for item in selected_items if item[0]]
        ))
        if not plan_ids or not payload.get("need_date"):
            raise ApiError("VALIDATION_REQUIRED", "生产计划和需求日期不能为空。", 422)
        inventory = InventoryService(self.env, self.user, self.company)
        plans = [inventory._plan(plan_id) for plan_id in sorted(plan_ids)]
        # Serialize requests for the same plans before reading committed quantities.
        self.env.cr.execute(
            "SELECT id FROM brew_production_plan WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(plan.id for plan in plans)],
        )
        for plan in plans:
            plan.invalidate_recordset()
            if plan.state not in {"checked", "stocking", "approved", "dispatched", "in_progress"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "当前生产计划不可生成采购需求。", 422)
        committed = self._committed_shortages(plan_ids)
        grouped = {}
        for plan in plans:
            result = inventory.plan_inventory(plan.id, reserve=False)
            factory_group = grouped.setdefault(plan.factory_id.id, {})
            for item in result["materials"]:
                if item["shortage"] <= 0:
                    continue
                product_id = int(item["product"]["id"])
                if selected_items and (plan.id, product_id) not in selected_items:
                    continue
                requestable_qty = max(
                    item["shortage"] - committed.get((plan.id, product_id), 0.0), 0.0
                )
                product = self._product(product_id)
                if float_is_zero(
                    requestable_qty, precision_rounding=product.uom_id.rounding
                ):
                    continue
                requested_qty = selected_items.get((plan.id, product_id)) if selected_items else None
                if requested_qty is None:
                    requested_qty = requestable_qty
                else:
                    try:
                        requested_qty = float(requested_qty)
                    except (TypeError, ValueError) as exc:
                        raise ApiError("VALIDATION_RANGE", "采购数量必须是大于零的有效数字。", 422) from exc
                    if not isfinite(requested_qty) or requested_qty <= 0:
                        raise ApiError("VALIDATION_RANGE", "采购数量必须是大于零的有效数字。", 422)
                values = factory_group.setdefault(
                    product_id,
                    {"quantity": 0.0, "allocations": [], "uom": item["uom"]},
                )
                values["quantity"] += requested_qty
                values["allocations"].append(
                    {
                        "plan": plan,
                        "required": item["required"],
                        "shortage": requested_qty,
                        "source_shortage": item["shortage"],
                    }
                )
        records = self.env["brew.purchase.requisition"]
        for factory_id, products in grouped.items():
            line_commands = []
            for product_id, values in products.items():
                product = self._product(product_id)
                allocation_commands = [
                    Command.create(
                        {
                            "production_plan_id": item["plan"].id,
                            "required_qty": item["required"],
                            "shortage_qty": item["shortage"],
                            "source_shortage_qty": item["source_shortage"],
                            "uom_id": product.uom_id.id,
                        }
                    )
                    for item in values["allocations"]
                ]
                line_commands.append(
                    Command.create(
                        {
                            "product_id": product.id,
                            "requested_qty": values["quantity"],
                            "uom_id": product.uom_id.id,
                            "allocation_ids": allocation_commands,
                        }
                    )
                )
            if line_commands:
                records |= self.env["brew.purchase.requisition"].sudo().create(
                    {
                        "company_id": self.company.id,
                        "factory_id": factory_id,
                        "need_date": payload["need_date"],
                        "request_type": "planned",
                        "reason": payload.get("reason") or "生产计划缺料汇总",
                        "requester_id": self.user.id,
                        "line_ids": line_commands,
                    }
                )
        if not records:
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "所选缺口已生成采购需求，请刷新后重新选择。",
                409,
            )
        for record in records:
            record.action_submit(self.user)
        return records, [self._serialize_requisition(item) for item in records]

    def requisition_action(self, record_id, action, payload):
        record = self._record("brew.purchase.requisition", record_id)
        self._check_version(record, payload.get("version"))
        if action == "submit":
            self._require_requisition_requester()
            record.action_submit(self.user)
        elif action == "approve":
            self._require_director()
            step = self._pending_approval_step(record.approval_instance_id)
            step.action_approve(self.user, payload.get('comment'))
            if record.approval_instance_id.state == 'approved':
                record.action_approve()
        elif action == "return":
            self._require_director()
            step = self._pending_approval_step(record.approval_instance_id)
            step.action_return(self.user, payload.get('reason'))
            record.action_return(payload.get("reason"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "采购需求动作不存在。", 404)
        return record, self._serialize_requisition(record)

    def _supplier(self, partner_id):
        partner = self.env["res.partner"].sudo().search(
            [
                ("id", "=", int(partner_id or 0)),
                ("supplier_rank", ">", 0),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not partner:
            raise ApiError("VALIDATION_RELATION", "供应商不存在或不可用。", 422)
        return partner

    @staticmethod
    def _approval_actor(user, role_code=None):
        if not user:
            return None
        roles = user.brew_role_payloads()
        role = next(
            (item for item in roles if item["code"] == role_code),
            roles[0] if roles else None,
        )
        return {
            "id": str(user.id),
            "name": user.name,
            "role": role,
            "roles": roles,
        }

    def _role_label(self, role_code):
        configuration = self.env["brew.role.configuration"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("code", "=", role_code),
                ("active", "=", True),
            ],
            limit=1,
        )
        if configuration:
            return configuration.name
        labels = {
            "requester": "发起人",
            "general_manager": "总经理",
            "production_director": "生产总监",
            "purchaser": "采购员",
            "finance": "财务",
            "tenant_admin": "管理员",
        }
        return labels.get(role_code, role_code or "审批人")

    def _approval_flow(self, instance):
        if not instance:
            return []
        return [
            {
                "sequence": step.sequence,
                "role_code": step.role_code,
                "role_label": self._role_label(step.role_code),
                "state": step.state,
                "assignee": self._approval_actor(step.assignee_id, step.role_code),
                "acted_by": self._approval_actor(step.acted_by_id, step.role_code),
                "acted_at": step.acted_at.isoformat() if step.acted_at else None,
                "comment": step.comment,
            }
            for step in instance.step_ids.sorted("sequence")
        ]

    def _approval_history(self, target_model, target_id):
        instances = self.env["brew.approval.instance"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("target_model", "=", target_model),
                ("target_id", "=", target_id),
            ],
            order="create_date,id",
        )
        history = []
        for cycle, instance in enumerate(instances, 1):
            history.append(
                {
                    "cycle": cycle,
                    "event": "submitted",
                    "state": "submitted",
                    "role_code": "requester",
                    "role_label": self._role_label("requester"),
                    "actor": self._approval_actor(instance.requester_id, "purchaser"),
                    "acted_at": (
                        instance.create_date.isoformat()
                        if instance.create_date
                        else None
                    ),
                    "comment": instance.submission_comment,
                    "instance_state": instance.state,
                }
            )
            for step in instance.step_ids.sorted("sequence"):
                history.append(
                    {
                        "cycle": cycle,
                        "event": "approval",
                        "state": step.state,
                        "role_code": step.role_code,
                        "role_label": self._role_label(step.role_code),
                        "actor": self._approval_actor(
                            step.acted_by_id or step.assignee_id,
                            step.role_code,
                        ),
                        "acted_at": (
                            step.acted_at.isoformat()
                            if step.acted_at
                            else None
                        ),
                        "comment": step.comment,
                        "instance_state": instance.state,
                    }
                )
        return history

    @staticmethod
    def _order_receipt_state(record):
        receipts = record.brew_receipt_ids.filtered(
            lambda item: item.state in {"arrived", "inspected", "done"}
        )
        if not receipts:
            return "not_received"
        ordered = sum(
            line.product_uom._compute_quantity(line.product_qty, line.product_id.uom_id)
            for line in record.order_line.filtered(lambda item: not item.display_type)
        )
        received = sum(
            line.uom_id._compute_quantity(line.received_qty, line.product_id.uom_id)
            for receipt in receipts
            for line in receipt.line_ids
        )
        if received + 1e-6 < ordered:
            return "partial" if received > 0 else "not_received"
        return "received" if all(item.state == "done" for item in receipts) else "receiving"

    @staticmethod
    def _received_quantities(record):
        quantities = {}
        for receipt in record.brew_receipt_ids.filtered(
            lambda item: item.state in {"arrived", "inspected", "done"}
        ):
            for line in receipt.line_ids:
                order_line = line.purchase_order_line_id
                quantities[order_line.id] = quantities.get(order_line.id, 0.0) + (
                    line.uom_id._compute_quantity(
                        line.received_qty, order_line.product_uom, round=False
                    )
                )
        return quantities

    def _serialize_order(self, record, include_edit_options=False):
        receipt_state = self._order_receipt_state(record)
        received_quantities = self._received_quantities(record)
        can_receive = record.state == "purchase" and any(
            received_quantities.get(line.id, 0.0) + line.product_uom.rounding
            < line.product_qty
            for line in record.order_line.filtered(lambda item: not item.display_type)
        )
        data = {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "approval_state": record.brew_approval_state,
            "approval_return_reason": record.brew_approval_return_reason,
            "factory": (
                {
                    "id": str(record.brew_factory_id.id),
                    "name": record.brew_factory_id.name,
                }
                if record.brew_factory_id
                else None
            ),
            "supplier": {
                "id": str(record.partner_id.id),
                "name": record.partner_id.name,
                "code": record.partner_id.brew_supplier_code,
            },
            "purchaser": (
                {"id": str(record.user_id.id), "name": record.user_id.name}
                if record.user_id else None
            ),
            "order_date": record.date_order.isoformat() if record.date_order else None,
            "expected_arrival_date": (
                record.brew_expected_arrival_date.isoformat()
                if record.brew_expected_arrival_date else None
            ),
            "receipt_state": receipt_state,
            "can_receive": can_receive,
            "currency": record.currency_id.name,
            "amount_untaxed": record.amount_untaxed,
            "amount_tax": record.amount_tax,
            "amount_total": record.amount_total,
            "placed_at": (
                record.brew_placed_at.isoformat() if record.brew_placed_at else None
            ),
            "close_reason": record.brew_close_reason,
            "procurement_type": (
                next(iter(set(record.brew_requisition_ids.mapped("request_type"))), "planned")
                if len(set(record.brew_requisition_ids.mapped("request_type"))) <= 1
                else "mixed"
            ),
            "requisition_ids": [str(item.id) for item in record.brew_requisition_ids],
            "requisitions": [
                {"id": str(item.id), "name": item.name}
                for item in record.brew_requisition_ids
            ],
            "receipts": [
                {
                    "id": str(receipt.id),
                    "name": receipt.name,
                    "state": receipt.state,
                    "completed_at": (
                        receipt.completed_at.isoformat() if receipt.completed_at else None
                    ),
                    "inspection_id": str(receipt.quality_inspection_id.id)
                    if receipt.quality_inspection_id
                    else None,
                    "inspection_name": receipt.quality_inspection_id.name
                    if receipt.quality_inspection_id
                    else "",
                    "inspection_state": receipt.quality_inspection_id.state
                    if receipt.quality_inspection_id
                    else None,
                }
                for receipt in record.brew_receipt_ids
            ],
            "returns": [
                {
                    "id": str(return_record.id),
                    "name": return_record.name,
                    "state": return_record.state,
                    "receipt_id": str(return_record.receipt_id.id)
                    if return_record.receipt_id
                    else None,
                }
                for return_record in self.env["brew.purchase.return"].sudo().search(
                    [("purchase_order_id", "=", record.id)], order="create_date,id"
                )
            ],
            "approval_instance_id": (
                str(record.brew_approval_instance_id.id)
                if record.brew_approval_instance_id
                else None
            ),
            "approval_flow": self._approval_flow(record.brew_approval_instance_id),
            "approval_history": self._approval_history(record._name, record.id),
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                        "brand": line.product_id.product_tmpl_id.brew_brand,
                        "specification": line.product_id.product_tmpl_id.brew_specification,
                    },
                    "quantity": line.product_qty,
                    "received_qty": received_quantities.get(line.id, 0.0),
                    "remaining_receipt_qty": max(
                        line.product_qty - received_quantities.get(line.id, 0.0), 0.0
                    ),
                    "uom": {
                        "id": str(line.product_uom.id),
                        "name": line.product_uom.name,
                    },
                    "price_unit": line.price_unit,
                    "subtotal": line.price_subtotal,
                    "package_spec": line.brew_package_spec,
                    "package_count": line.brew_package_count,
                    "planned_date": (
                        line.date_planned.isoformat() if line.date_planned else None
                    ),
                    "requisition_line_ids": [
                        str(item.id) for item in line.brew_requisition_line_ids
                    ],
                }
                for line in record.order_line.filtered(lambda item: not item.display_type)
            ],
            "version": self._version(record),
        }
        if include_edit_options and record.state in {"draft", "sent"} and record.brew_approval_state in {"none", "returned"}:
            request_types = set(record.brew_requisition_ids.mapped("request_type"))
            current_source_ids = set(record.order_line.mapped("brew_requisition_line_ids").ids)
            domain = [
                ("company_id", "=", self.company.id),
                ("requisition_id.factory_id", "=", record.brew_factory_id.id),
                ("requisition_id.state", "=", "approved"),
            ]
            if len(request_types) == 1:
                domain.append(("requisition_id.request_type", "=", next(iter(request_types))))
            candidates = self.env["brew.purchase.requisition.line"].sudo().search(domain, order="requisition_id desc,id")
            data["editable_requisition_lines"] = [
                {
                    "id": str(line.id),
                    "requisition_id": str(line.requisition_id.id),
                    "requisition_name": line.requisition_id.name,
                    "request_type": line.requisition_id.request_type,
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                        "brand": line.product_id.product_tmpl_id.brew_brand,
                        "specification": line.product_id.product_tmpl_id.brew_specification,
                    },
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "available_qty": line.approved_qty - line.ordered_qty,
                    "price_unit": line.product_id.standard_price,
                    "package_spec": line.product_id.product_tmpl_id.brew_specification,
                }
                for line in candidates
                if line.id not in current_source_ids
                and line.approved_qty - line.ordered_qty > line.uom_id.rounding
            ]
        return data

    def orders(
        self, page, page_size, state=None, supplier_name=None, order_no=None,
        receipt_state=None
    ):
        domain = [
            ("company_id", "=", self.company.id),
            ("brew_factory_id", "in", self._factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        if supplier_name:
            domain.append(("partner_id.name", "ilike", supplier_name))
        if order_no:
            domain.append(("name", "ilike", order_no))
        model = self.env["purchase.order"].sudo()
        records = model.search(domain, order="date_order desc,id desc")
        if receipt_state:
            records = records.filtered(
                lambda item: self._order_receipt_state(item) == receipt_state
            )
        total = len(records)
        records = records[(page - 1) * page_size:page * page_size]
        return [self._serialize_order(item) for item in records], total

    def _order_line_values(self, values, supplier, factory):
        product = self._product(values.get("product_id"))
        uom = self._uom(values.get("uom_id") or product.uom_id.id, product)
        quantity = float(values.get("quantity") or 0)
        if quantity <= 0:
            raise ApiError("VALIDATION_RANGE", "采购订单数量必须大于零。", 422)
        return {
            "product_id": product.id,
            "name": values.get("description") or product.display_name,
            "product_qty": quantity,
            "product_uom": uom.id,
            "price_unit": float(values.get("price_unit", product.standard_price) or 0),
            "brew_package_spec": values.get("package_spec") or product.product_tmpl_id.brew_specification,
            "brew_package_count": float(values.get("package_count") or quantity),
            "date_planned": self._datetime_value(
                values.get("planned_date"), fields.Datetime.to_string(fields.Datetime.now())
            ),
            "brew_requisition_line_ids": [
                Command.set(
                    [int(item) for item in values.get("requisition_line_ids") or []]
                )
            ],
        }

    def create_order(self, payload):
        self._require_purchase()
        factory = self._factory(payload.get("factory_id"))
        supplier = self._supplier(payload.get("supplier_id"))
        lines = payload.get("lines") or []
        if not lines:
            raise ApiError("VALIDATION_REQUIRED", "采购订单明细不能为空。", 422)
        line_values = [
            Command.create(self._order_line_values(item, supplier, factory))
            for item in lines
        ]
        requisition_ids = list(
            dict.fromkeys(int(item) for item in payload.get("requisition_ids") or [])
        )
        if not requisition_ids:
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "采购订单必须由已批准的采购需求生成。",
                422,
            )
        requisitions = self.env["brew.purchase.requisition"].sudo().search(
            [
                ("id", "in", requisition_ids),
                ("company_id", "=", self.company.id),
                ("factory_id", "=", factory.id),
                ("state", "=", "approved"),
            ]
        )
        if set(requisitions.ids) != set(requisition_ids):
            raise ApiError("STATE_PRECONDITION_FAILED", "采购需求无效或尚未批准。", 422)
        if len(set(requisitions.mapped("request_type"))) > 1:
            raise ApiError("VALIDATION_RELATION", "计划采购与紧急采购不能合并到同一订单。", 422)
        valid_requisition_line_ids = set(requisitions.mapped("line_ids").ids)
        for values in lines:
            source_ids = {
                int(item) for item in values.get("requisition_line_ids") or []
            }
            if not source_ids or not source_ids.issubset(valid_requisition_line_ids):
                raise ApiError(
                    "VALIDATION_RELATION",
                    "采购订单每条明细都必须关联本单已批准的需求明细。",
                    422,
                )
        order = self.env["purchase.order"].sudo().create(
            {
                "company_id": self.company.id,
                "partner_id": supplier.id,
                "user_id": self.user.id,
                "brew_factory_id": factory.id,
                "date_order": payload.get("order_date") or fields.Datetime.now(),
                "brew_expected_arrival_date": payload.get("expected_arrival_date") or False,
                "brew_requisition_ids": [Command.set(requisitions.ids)],
                "order_line": line_values,
            }
        )
        for line in order.order_line:
            for requisition_line in line.brew_requisition_line_ids:
                quantity = line.product_uom._compute_quantity(
                    line.product_qty, requisition_line.uom_id
                )
                remaining = requisition_line.approved_qty - requisition_line.ordered_qty
                if quantity > remaining + requisition_line.uom_id.rounding:
                    raise ApiError(
                        "VALIDATION_RANGE",
                        f"{requisition_line.product_id.display_name} 下单数量超过未下单批准量。",
                        422,
                    )
                requisition_line.ordered_qty += quantity
        return order, self._serialize_order(order)

    def create_order_from_requisitions(self, payload):
        self._require_purchase()
        line_requests = payload.get("lines") or []
        if not line_requests:
            raise ApiError("VALIDATION_REQUIRED", "请选择采购需求明细。", 422)
        grouped = {}
        for values in line_requests:
            line = self.env["brew.purchase.requisition.line"].sudo().search(
                [
                    ("id", "=", int(values.get("requisition_line_id") or 0)),
                    ("company_id", "=", self.company.id),
                    ("requisition_id.state", "=", "approved"),
                ],
                limit=1,
            )
            if not line:
                raise ApiError("STATE_PRECONDITION_FAILED", "采购需求明细无效。", 422)
            supplier = self._supplier(
                values.get("supplier_id")
                or payload.get("supplier_id")
                or line.requisition_id.preferred_supplier_id.id
                or line.product_id.product_tmpl_id.brew_supplier_id.id
            )
            quantity = float(
                values.get("quantity")
                or line.approved_qty - line.ordered_qty
            )
            remaining = line.approved_qty - line.ordered_qty
            if quantity <= 0 or quantity > remaining + line.uom_id.rounding:
                raise ApiError(
                    "VALIDATION_RANGE",
                    f"{line.product_id.display_name} 下单数量无效或超过未下单批准量。",
                    422,
                )
            key = (
                supplier.id,
                line.requisition_id.factory_id.id,
                line.requisition_id.request_type,
            )
            group = grouped.setdefault(
                key,
                {"requisition_ids": set(), "lines": []},
            )
            group["requisition_ids"].add(line.requisition_id.id)
            group["lines"].append(
                {
                    "product_id": line.product_id.id,
                    "uom_id": line.uom_id.id,
                    "quantity": quantity,
                    "price_unit": values.get("price_unit", line.product_id.standard_price),
                    "planned_date": values.get("planned_date"),
                    "package_spec": values.get("package_spec"),
                    "package_count": values.get("package_count"),
                    "requisition_line_ids": [line.id],
                }
            )
        orders = self.env["purchase.order"]
        serialized = []
        for (supplier_id, factory_id, _request_type), group in grouped.items():
            order, data = self.create_order(
                {
                    "factory_id": factory_id,
                    "supplier_id": supplier_id,
                    "order_date": payload.get("order_date"),
                    "expected_arrival_date": payload.get("expected_arrival_date"),
                    "requisition_ids": list(group["requisition_ids"]),
                    "lines": group["lines"],
                }
            )
            orders |= order
            serialized.append(data)
        return orders, serialized

    def update_order(self, order_id, payload):
        self._require_purchase()
        order = self._record("purchase.order", order_id, "brew_factory_id")
        self._check_version(order, payload.get("version"))
        if order.state not in {"draft", "sent"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "当前采购订单不可修改。", 422)
        values = {}
        if "supplier_id" in payload:
            values["partner_id"] = self._supplier(payload["supplier_id"]).id
        if "order_date" in payload:
            values["date_order"] = payload["order_date"]
        if "expected_arrival_date" in payload:
            values["brew_expected_arrival_date"] = (
                payload["expected_arrival_date"] or False
            )
        line_payloads = payload.get("lines") if "lines" in payload else None
        if line_payloads is not None:
            if not line_payloads:
                raise ApiError("VALIDATION_REQUIRED", "采购订单明细不能为空。", 422)
            order_lines = order.order_line.filtered(lambda item: not item.display_type)
            order_lines_by_id = {line.id: line for line in order_lines}
            current_source_qty = {}
            for line in order_lines:
                sources = line.brew_requisition_line_ids
                if len(sources) != 1:
                    raise ApiError("VALIDATION_RELATION", "采购订单明细必须且只能关联一条采购需求明细。", 422)
                source = sources[0]
                current_source_qty[source.id] = current_source_qty.get(source.id, 0.0) + line.product_uom._compute_quantity(
                    line.product_qty, source.uom_id, round=False
                )

            retained_line_ids = set()
            retained_source_ids = set()
            desired = []
            request_types = set(order.brew_requisition_ids.mapped("request_type"))
            for item in line_payloads:
                line_id = int(item.get("id") or 0)
                if line_id:
                    if line_id in retained_line_ids or line_id not in order_lines_by_id:
                        raise ApiError("VALIDATION_RELATION", "采购订单明细与当前订单不一致。", 422)
                    retained_line_ids.add(line_id)
                    line = order_lines_by_id[line_id]
                    source = line.brew_requisition_line_ids[0]
                else:
                    line = None
                    source = self.env["brew.purchase.requisition.line"].sudo().search(
                        [
                            ("id", "=", int(item.get("requisition_line_id") or 0)),
                            ("company_id", "=", self.company.id),
                            ("requisition_id.factory_id", "=", order.brew_factory_id.id),
                            ("requisition_id.state", "=", "approved"),
                        ],
                        limit=1,
                    )
                    if not source:
                        raise ApiError("STATE_PRECONDITION_FAILED", "新增的采购需求明细无效或已不可下单。", 422)
                if source.id in retained_source_ids:
                    raise ApiError("VALIDATION_RELATION", "同一采购需求明细不能重复加入采购订单。", 422)
                retained_source_ids.add(source.id)
                if request_types and source.requisition_id.request_type not in request_types:
                    raise ApiError("VALIDATION_RELATION", "计划采购与紧急采购不能合并到同一订单。", 422)
                quantity = float(item.get("quantity") or 0)
                package_count = float(item.get("package_count") or 0)
                price_unit = float(item.get("price_unit") or 0)
                if quantity <= 0 or package_count <= 0 or price_unit < 0:
                    raise ApiError(
                        "VALIDATION_RANGE",
                        "采购数量、件数必须大于零，采购单价不能小于零。",
                        422,
                    )
                target_uom = line.product_uom if line else source.uom_id
                source_quantity = target_uom._compute_quantity(quantity, source.uom_id, round=False)
                available = source.approved_qty - source.ordered_qty + current_source_qty.get(source.id, 0.0)
                if source_quantity > available + source.uom_id.rounding:
                    raise ApiError(
                        "VALIDATION_RANGE",
                        f"{source.product_id.display_name} 下单数量超过未下单批准量。",
                        422,
                    )
                desired.append(
                    {
                        "line": line,
                        "source": source,
                        "source_quantity": source_quantity,
                        "values": {
                            "product_qty": quantity,
                            "brew_package_count": package_count,
                            "brew_package_spec": item.get("package_spec") or False,
                            "price_unit": price_unit,
                            "date_planned": self._datetime_value(
                                item.get("planned_date"),
                                line.date_planned if line else fields.Datetime.now(),
                            ),
                        },
                    }
                )

            for source_id, quantity in current_source_qty.items():
                source = self.env["brew.purchase.requisition.line"].sudo().browse(source_id)
                source.ordered_qty = max(source.ordered_qty - quantity, 0.0)
            (order_lines - order_lines.filtered(lambda line: line.id in retained_line_ids)).unlink()
            requisitions = self.env["brew.purchase.requisition"]
            for item in desired:
                line = item["line"]
                source = item["source"]
                if line:
                    line.write(item["values"])
                else:
                    create_values = dict(item["values"], order_id=order.id)
                    create_values.update(
                        {
                            "product_id": source.product_id.id,
                            "name": source.product_id.display_name,
                            "product_uom": source.uom_id.id,
                            "brew_requisition_line_ids": [Command.set([source.id])],
                        }
                    )
                    self.env["purchase.order.line"].sudo().create(create_values)
                source.ordered_qty += item["source_quantity"]
                requisitions |= source.requisition_id
            values["brew_requisition_ids"] = [Command.set(requisitions.ids)]
        if not values:
            if line_payloads is None:
                raise ApiError("VALIDATION_REQUIRED", "没有可更新的采购订单字段。", 422)
        else:
            order.write(values)
        if payload.get("submit_after_save") is True:
            order.action_brew_place()
        return order, self._serialize_order(order)

    def order_action(self, order_id, action, payload):
        order = self._record("purchase.order", order_id, "brew_factory_id")
        self._check_version(order, payload.get("version"))
        if action == "submit":
            self._require_purchase()
            order.action_brew_place()
        elif action == "approve":
            self._require_director()
            step = self._pending_approval_step(order.brew_approval_instance_id)
            step.action_approve(self.user, payload.get('comment'))
            if order.brew_approval_instance_id.state == 'approved':
                order.action_brew_approve()
        elif action == "place":
            self._require_purchase()
            order.action_brew_place()
        elif action == "close":
            self._require_purchase()
            order.action_brew_close(payload.get("reason"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "采购订单动作不存在。", 404)
        return order, self._serialize_order(order)

    def _serialize_shipment(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "purchase_order_id": str(record.purchase_order_id.id),
            "purchase_order_name": record.purchase_order_id.name,
            "carrier_name": record.carrier_name,
            "tracking_number": record.tracking_number,
            "shipped_at": record.shipped_at.isoformat(),
            "expected_arrival": (
                record.expected_arrival.isoformat() if record.expected_arrival else None
            ),
            "actual_arrival": (
                record.actual_arrival.isoformat() if record.actual_arrival else None
            ),
            "lines": [
                {
                    "order_line_id": str(line.purchase_order_line_id.id),
                    "product_name": line.product_id.display_name,
                    "quantity": line.shipped_qty,
                    "uom": line.uom_id.name,
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def create_shipment(self, order_id, payload):
        self._require_purchase()
        order = self._record("purchase.order", order_id, "brew_factory_id")
        if order.state != "purchase":
            raise ApiError("STATE_PRECONDITION_FAILED", "采购订单尚未正式下单。", 422)
        commands = []
        for values in payload.get("lines") or []:
            line = order.order_line.filtered(
                lambda item: item.id == int(values.get("order_line_id") or 0)
            )
            quantity = float(values.get("quantity") or 0)
            if not line or quantity <= 0:
                raise ApiError("VALIDATION_RELATION", "发货明细无效。", 422)
            commands.append(
                Command.create(
                    {
                        "purchase_order_line_id": line.id,
                        "shipped_qty": quantity,
                    }
                )
            )
        if not commands or not payload.get("carrier_name") or not payload.get("tracking_number"):
            raise ApiError("VALIDATION_REQUIRED", "物流信息和发货明细不能为空。", 422)
        shipment = self.env["brew.supplier.shipment"].sudo().create(
            {
                "purchase_order_id": order.id,
                "carrier_name": payload["carrier_name"],
                "tracking_number": payload["tracking_number"],
                "shipped_at": payload.get("shipped_at") or fields.Datetime.now(),
                "expected_arrival": payload.get("expected_arrival"),
                "line_ids": commands,
            }
        )
        return shipment, self._serialize_shipment(shipment)

    def _location(self, location_id, factory):
        location = self.env["stock.location"].sudo().search(
            [
                ("id", "=", int(location_id or 0)),
                ("company_id", "=", self.company.id),
                ("usage", "=", "internal"),
                ("brew_factory_id", "=", factory.id),
            ],
            limit=1,
        )
        if not location:
            raise ApiError("VALIDATION_RELATION", "收货库位无效。", 422)
        return location

    def _receipt_destination(self, location_id, factory):
        location = self._location(location_id, factory)
        if location.brew_location_type in {"qc", "material_issue"}:
            raise ApiError("VALIDATION_RELATION", "请选择原料仓或包装物仓作为入库库位。", 422)
        return location

    def _serialize_receipt(self, record):
        order = record.purchase_order_id
        supplier = order.partner_id
        lines_data = []
        total_received = 0.0
        total_qualified = 0.0
        total_rejected = 0.0
        inspection = record.quality_inspection_id
        inspection_map = {}
        if inspection:
            for il in inspection.line_ids:
                inspection_map[il.receipt_line_id.id] = il
        return_lines = self.env["brew.purchase.return.line"].sudo().search(
            [
                ("purchase_order_line_id", "in", record.line_ids.mapped("purchase_order_line_id").ids),
                ("return_id.state", "not in", ["cancelled"]),
            ]
        )
        returned_map = {}
        for rl in return_lines:
            key = (rl.purchase_order_line_id.id, rl.lot_id.id if rl.lot_id else None)
            returned_map[key] = returned_map.get(key, 0.0) + rl.return_qty
        for line in record.line_ids:
            ol = line.purchase_order_line_id
            insp = inspection_map.get(line.id)
            received = line.received_qty
            qualified = insp.qualified_qty if insp else 0
            rejected = insp.rejected_qty if insp else 0
            total_received += received
            total_qualified += qualified
            total_rejected += rejected
            rkey = (ol.id, line.lot_id.id if line.lot_id else None)
            returned_qty = returned_map.get(rkey, 0.0)
            stock_available_qty = 0.0
            if line.lot_id:
                stock_available_qty = self.env["stock.quant"].sudo()._get_available_quantity(
                    line.product_id,
                    line.destination_location_id or record.destination_location_id,
                    lot_id=line.lot_id,
                    strict=False,
                )
            returnable_qty = max(
                min(received - returned_qty, stock_available_qty),
                0.0,
            )
            lines_data.append(
                {
                    "id": str(line.id),
                    "order_line_id": str(ol.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                        "brand": getattr(line.product_id.product_tmpl_id, "brew_brand", "") or "",
                        "specification": getattr(line.product_id.product_tmpl_id, "brew_specification", "") or "",
                        "material_category": getattr(line.product_id.product_tmpl_id, "brew_material_category", "") or "",
                    },
                    "received_qty": received,
                    "ordered_qty": ol.product_qty,
                    "price_unit": ol.price_unit,
                    "package_spec": getattr(ol, "brew_package_spec", "") or "",
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "lot_name": line.lot_name,
                    "supplier_lot": line.supplier_lot,
                    "expiration_date": (
                        line.expiration_date.isoformat()
                        if line.expiration_date
                        else None
                    ),
                    "lot_id": str(line.lot_id.id) if line.lot_id else None,
                    "destination_location": {
                        "id": str((line.destination_location_id or record.destination_location_id).id),
                        "name": (line.destination_location_id or record.destination_location_id).complete_name,
                    },
                    "qualified_qty": qualified,
                    "rejected_qty": rejected,
                    "returned_qty": returned_qty,
                    "stock_available_qty": stock_available_qty,
                    "returnable_qty": returnable_qty,
                    "note": "",
                }
            )
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "purchase_order": {
                "id": str(order.id),
                "name": order.name,
                "state": order.state,
                "date_order": order.date_order.isoformat() if order.date_order else None,
                "amount_total": order.amount_total,
                "currency": order.currency_id.name if order.currency_id else "",
            },
            "supplier": {
                "id": str(supplier.id),
                "name": supplier.name,
                "code": getattr(supplier, "brew_supplier_code", "") or "",
            },
            "purchaser": (
                {"id": str(order.user_id.id), "name": order.user_id.name}
                if order.user_id
                else None
            ),
            "shipment_id": str(record.shipment_id.id) if record.shipment_id else None,
            "qc_location": {
                "id": str(record.qc_location_id.id),
                "name": record.qc_location_id.complete_name,
            },
            "destination_location": {
                "id": str(record.destination_location_id.id),
                "name": record.destination_location_id.complete_name,
            },
            "picking_id": str(record.picking_id.id) if record.picking_id else None,
            "approval_instance_id": (
                str(record.approval_instance_id.id)
                if record.approval_instance_id
                else None
            ),
            "approval_flow": self._approval_flow(record.approval_instance_id),
            "approval_state": record.approval_instance_id.state if record.approval_instance_id else None,
            "approval_history": self._approval_history(record._name, record.id),
            "quality_inspection_id": (
                str(record.quality_inspection_id.id)
                if record.quality_inspection_id
                else None
            ),
            "arrived_at": record.arrived_at.isoformat() if record.arrived_at else None,
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at else None
            ),
            "create_date": record.create_date.isoformat() if record.create_date else None,
            "creator": record.create_uid.name if record.create_uid else "",
            "write_date": record.write_date.isoformat() if record.write_date else None,
            "note": record.note or "",
            "return_reason": record.return_reason or "",
            "total_received": total_received,
            "total_qualified": total_qualified,
            "total_rejected": total_rejected,
            "lines": lines_data,
            "version": self._version(record),
        }

    def receipts(self, page, page_size, filters=None):
        filters = filters or {}
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("keyword"):
            kw = filters["keyword"]
            domain.append(
                [
                    "|", "|",
                    ("name", "ilike", kw),
                    ("purchase_order_id.name", "ilike", kw),
                    ("line_ids.product_id", "ilike", kw),
                ]
            )
        if filters.get("supplier_name"):
            domain.append(("purchase_order_id.partner_id.name", "ilike", filters["supplier_name"]))
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("date_from"):
            domain.append(("create_date", ">=", filters["date_from"] + " 00:00:00"))
        if filters.get("date_to"):
            domain.append(("create_date", "<=", filters["date_to"] + " 23:59:59"))
        model = self.env["brew.receipt"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_receipt(item) for item in records], total

    def create_receipt(self, payload):
        self._require_warehouse()
        order = self._record(
            "purchase.order", payload.get("purchase_order_id"), "brew_factory_id"
        )
        if order.state != "purchase":
            raise ApiError("STATE_PRECONDITION_FAILED", "采购订单尚未正式下单。", 422)
        qc_location = self._location(payload.get("qc_location_id"), order.brew_factory_id)
        destination = self._receipt_destination(
            payload.get("destination_location_id"), order.brew_factory_id
        )
        if qc_location.brew_location_type != "qc":
            raise ApiError("VALIDATION_RELATION", "到货必须先进入待检库位。", 422)
        shipment = self.env["brew.supplier.shipment"].sudo()
        if payload.get("shipment_id"):
            shipment = shipment.search(
                [
                    ("id", "=", int(payload["shipment_id"])),
                    ("purchase_order_id", "=", order.id),
                    ("state", "in", ["shipped", "in_transit"]),
                ],
                limit=1,
            )
            if not shipment:
                raise ApiError("VALIDATION_RELATION", "供应商发货记录无效。", 422)
        receipt_name = (
            self.env["ir.sequence"].sudo().next_by_code("brew.receipt") or "New"
        )
        commands = []
        seen_order_lines = set()
        for index, values in enumerate(payload.get("lines") or [], 1):
            order_line = order.order_line.filtered(
                lambda item: item.id == int(values.get("order_line_id") or 0)
            )
            quantity = float(values.get("received_qty") or 0)
            if not order_line or quantity <= 0:
                raise ApiError("VALIDATION_REQUIRED", "收货明细和数量不能为空。", 422)
            if order_line.id in seen_order_lines:
                raise ApiError("VALIDATION_RELATION", "同一采购明细不能重复登记。", 422)
            seen_order_lines.add(order_line.id)
            line_destination = self._receipt_destination(
                values.get("destination_location_id") or destination.id,
                order.brew_factory_id,
            )
            lot_name = f"{receipt_name}-{index:02d}"
            commands.append(
                Command.create(
                    {
                        "purchase_order_line_id": order_line.id,
                        "destination_location_id": line_destination.id,
                        "received_qty": quantity,
                        "lot_name": lot_name,
                        "supplier_lot": values.get("supplier_lot"),
                        "expiration_date": values.get("expiration_date"),
                    }
                )
            )
        if not commands:
            raise ApiError("VALIDATION_REQUIRED", "收货明细不能为空。", 422)
        with self.env.cr.savepoint():
            receipt = self.env["brew.receipt"].sudo().create(
                {
                    "name": receipt_name,
                    "purchase_order_id": order.id,
                    "shipment_id": shipment.id or False,
                    "qc_location_id": qc_location.id,
                    "destination_location_id": destination.id,
                    "line_ids": commands,
                }
            )
            if payload.get("confirm_arrival") is True:
                receipt.action_confirm_arrival(self.user)
        return receipt, self._serialize_receipt(receipt)

    def update_receipt(self, receipt_id, payload):
        self._require_warehouse()
        receipt = self._record("brew.receipt", receipt_id)
        approval_returned = (
            receipt.state == "pending_approval"
            and receipt.approval_instance_id
            and receipt.approval_instance_id.state == "returned"
        )
        if (
            (receipt.state != "draft" and not approval_returned)
            or receipt.picking_id
            or receipt.quality_inspection_id
        ):
            raise ApiError(
                "STATE_PRECONDITION_FAILED",
                "只有草稿或审批退回且尚未到货的收货单可以编辑。",
                422,
            )
        self._check_version(receipt, payload.get("version"))

        vals = {}
        if "qc_location_id" in payload:
            qc_location = self._location(payload.get("qc_location_id"), receipt.factory_id)
            if qc_location.brew_location_type != "qc":
                raise ApiError("VALIDATION_RELATION", "到货必须先进入待检库位。", 422)
            vals["qc_location_id"] = qc_location.id
        if "destination_location_id" in payload:
            destination = self._receipt_destination(payload.get("destination_location_id"), receipt.factory_id)
            vals["destination_location_id"] = destination.id
        if "note" in payload:
            vals["note"] = payload.get("note")

        if "lines" in payload:
            incoming = payload.get("lines") or []
            if not incoming:
                raise ApiError("VALIDATION_REQUIRED", "收货明细不能为空。", 422)
            existing_by_id = {line.id: line for line in receipt.line_ids}
            used_order_line_ids = set()
            used_lot_names = set(receipt.line_ids.mapped("lot_name"))
            commands = []
            next_lot_index = len(receipt.line_ids) + 1
            for values in incoming:
                existing = None
                if values.get("id"):
                    existing = existing_by_id.pop(int(values["id"]), None)
                    if not existing:
                        raise ApiError("VALIDATION_RELATION", "收货明细不属于当前收货单。", 422)
                order_line_id = int(
                    values.get("order_line_id")
                    or (existing.purchase_order_line_id.id if existing else 0)
                )
                order_line = receipt.purchase_order_id.order_line.filtered(
                    lambda line: line.id == order_line_id and not line.display_type
                )
                quantity = float(values.get("received_qty") or 0)
                if not order_line or quantity <= 0:
                    raise ApiError("VALIDATION_REQUIRED", "收货明细和数量不能为空。", 422)
                if order_line_id in used_order_line_ids:
                    raise ApiError("VALIDATION_RELATION", "同一采购明细不能重复登记。", 422)
                used_order_line_ids.add(order_line_id)
                destination_id = values.get("destination_location_id") or (
                    existing.destination_location_id.id if existing and existing.destination_location_id
                    else vals.get("destination_location_id", receipt.destination_location_id.id)
                )
                line_destination = self._receipt_destination(destination_id, receipt.factory_id)
                line_values = {
                    "purchase_order_line_id": order_line_id,
                    "destination_location_id": line_destination.id,
                    "received_qty": quantity,
                    "supplier_lot": values.get("supplier_lot"),
                    "expiration_date": values.get("expiration_date") or False,
                }
                if existing:
                    commands.append(Command.update(existing.id, line_values))
                else:
                    lot_name = "%s-%02d" % (receipt.name, next_lot_index)
                    while lot_name in used_lot_names:
                        next_lot_index += 1
                        lot_name = "%s-%02d" % (receipt.name, next_lot_index)
                    line_values["lot_name"] = lot_name
                    used_lot_names.add(lot_name)
                    next_lot_index += 1
                    commands.append(Command.create(line_values))
            commands.extend(Command.delete(line.id) for line in existing_by_id.values())
            vals["line_ids"] = commands

        with self.env.cr.savepoint():
            if vals:
                receipt.write(vals)
            if payload.get("confirm_arrival") is True:
                receipt.action_confirm_arrival(self.user)
        return receipt, self._serialize_receipt(receipt)

    def receipt_action(self, receipt_id, action, payload):
        self._require_warehouse()
        receipt = self._record("brew.receipt", receipt_id)
        self._check_version(receipt, payload.get("version"))
        if action == "confirm-arrival":
            receipt.action_confirm_arrival(self.user)
        elif action == "put-away":
            receipt.action_put_away()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "收货动作不存在。", 404)
        return receipt, self._serialize_receipt(receipt)

    def _serialize_inspection(self, record):
        receipt = record.receipt_id
        order = receipt.purchase_order_id if receipt else self.env["purchase.order"]
        supplier = order.partner_id if order else self.env["res.partner"]
        lines_data = []
        for line in record.line_ids:
            rl = line.receipt_line_id
            ol = rl.purchase_order_line_id if rl else self.env["purchase.order.line"]
            tmpl = line.product_id.product_tmpl_id
            lines_data.append(
                {
                    "id": str(line.id),
                    "receipt_line_id": str(rl.id) if rl else None,
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                        "brand": getattr(tmpl, "brew_brand", "") or "",
                        "specification": getattr(tmpl, "brew_specification", "") or "",
                        "material_category": getattr(tmpl, "brew_material_category", "") or "",
                    },
                    "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name},
                    "inspected_qty": line.inspected_qty,
                    "qualified_qty": line.qualified_qty,
                    "rejected_qty": line.rejected_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "disposition": line.disposition,
                    "result": line.result_json,
                    "package_spec": getattr(ol, "brew_package_spec", "") or "",
                    "supplier_lot": rl.supplier_lot if rl else "",
                    "expiration_date": (
                        rl.expiration_date.isoformat()
                        if rl and rl.expiration_date
                        else None
                    ),
                    "price_unit": ol.price_unit if ol else 0,
                    "note": line.note,
                }
            )
        total_inspected = sum(line.inspected_qty for line in record.line_ids)
        total_qualified = sum(line.qualified_qty for line in record.line_ids)
        total_rejected = sum(line.rejected_qty for line in record.line_ids)
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "receipt_id": str(receipt.id) if receipt else None,
            "receipt_name": receipt.name if receipt else "",
            "receipt_state": receipt.state if receipt else "",
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "purchase_order": {
                "id": str(order.id),
                "name": order.name,
                "state": order.state,
                "date_order": order.date_order.isoformat() if order.date_order else None,
            } if order else None,
            "supplier": {
                "id": str(supplier.id),
                "name": supplier.name,
                "code": getattr(supplier, "brew_supplier_code", "") or "",
            } if supplier else None,
            "inspector": (
                {"id": str(record.inspector_id.id), "name": record.inspector_id.name}
                if record.inspector_id
                else None
            ),
            "inspected_at": (
                record.inspected_at.isoformat() if record.inspected_at else None
            ),
            "approval_state": (
                record.approval_instance_id.state
                if record.approval_instance_id
                else "none"
            ),
            "approval_history": self._approval_history(record._name, record.id),
            "return_reason": record.return_reason or "",
            "create_date": record.create_date.isoformat() if record.create_date else None,
            "creator": record.create_uid.name if record.create_uid else "",
            "write_date": record.write_date.isoformat() if record.write_date else None,
            "note": record.note,
            "total_inspected": total_inspected,
            "total_qualified": total_qualified,
            "total_rejected": total_rejected,
            "lines": lines_data,
            "version": self._version(record),
        }

    def inspections(self, page, page_size, filters=None):
        filters = filters or {}
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if filters.get("state") == "pending":
            domain.append(("state", "in", ["draft", "submitted", "returned"]))
        elif filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("keyword"):
            kw = filters["keyword"]
            domain.append(
                [
                    "|", "|",
                    ("name", "ilike", kw),
                    ("receipt_id.name", "ilike", kw),
                    ("line_ids.product_id", "ilike", kw),
                ]
            )
        if filters.get("supplier_name"):
            domain.append(("receipt_id.purchase_order_id.partner_id.name", "ilike", filters["supplier_name"]))
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("date_from"):
            domain.append(("create_date", ">=", filters["date_from"] + " 00:00:00"))
        if filters.get("date_to"):
            domain.append(("create_date", "<=", filters["date_to"] + " 23:59:59"))
        model = self.env["brew.quality.inspection"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_inspection(item) for item in records], total

    def inspection_for_receipt(self, payload):
        self._require_warehouse()
        receipt = self._record("brew.receipt", payload.get("receipt_id"))
        inspection = receipt.quality_inspection_id
        if not inspection:
            raise ApiError("STATE_PRECONDITION_FAILED", "收货尚未确认到货。", 422)
        return inspection, self._serialize_inspection(inspection)

    def update_inspection_results(self, inspection_id, payload):
        self._require_warehouse()
        inspection = self._record(
            "brew.quality.inspection", inspection_id
        )
        self._check_version(inspection, payload.get("version"))
        if inspection.state not in {"draft", "returned", "submitted"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有尚未完成的质检单可以修改。", 422)
        with self.env.cr.savepoint():
            by_id = {line.id: line for line in inspection.line_ids}
            incoming = payload.get("lines") or []
            if not incoming:
                raise ApiError("VALIDATION_REQUIRED", "质检结果不能为空。", 422)
            incoming_ids = [int(values.get("id") or 0) for values in incoming]
            if len(set(incoming_ids)) != len(incoming_ids):
                raise ApiError("VALIDATION_RELATION", "质检明细不能重复。", 422)
            if payload.get("complete_and_put_away") is True and set(incoming_ids) != set(by_id):
                raise ApiError("VALIDATION_REQUIRED", "请填写全部质检明细后再确认入库。", 422)
            for values in incoming:
                line = by_id.get(int(values.get("id") or 0))
                if not line:
                    raise ApiError("VALIDATION_RELATION", "质检明细不存在。", 422)
                qualified = float(values.get("qualified_qty") or 0)
                rejected = float(values.get("rejected_qty") or 0)
                if not isfinite(qualified) or not isfinite(rejected) or qualified < 0 or rejected < 0:
                    raise ApiError("VALIDATION_RANGE", "质检数量不能小于零。", 422)
                if qualified + rejected > float(line.inspected_qty) + float(line.uom_id.rounding):
                    raise ApiError("VALIDATION_RANGE", f"{line.product_id.display_name} 的合格与不合格数量之和不能超过检验数量 {line.inspected_qty}。", 422)
                if rejected and not values.get("disposition"):
                    raise ApiError("VALIDATION_REQUIRED", f"{line.product_id.display_name} 存在不合格数量，请选择退货或报损。", 422)
                line.write(
                    {
                        "qualified_qty": qualified,
                        "rejected_qty": rejected,
                        "disposition": values.get("disposition") or False,
                        "result_json": values.get("result_json") or "{}",
                        "note": values.get("note"),
                    }
                )
            if "note" in payload:
                inspection.note = payload["note"]
            if payload.get("complete_and_put_away") is True:
                inspection.action_complete(self.user)
        return inspection, self._serialize_inspection(inspection)

    def inspection_action(self, inspection_id, action, payload):
        self._require_warehouse()
        inspection = self._record("brew.quality.inspection", inspection_id)
        self._check_version(inspection, payload.get("version"))
        if action not in {"submit", "complete"}:
            raise ApiError("RESOURCE_NOT_FOUND", "质检动作不存在。", 404)
        with self.env.cr.savepoint():
            inspection.action_complete(self.user)
        return inspection, self._serialize_inspection(inspection)

    # ── Purchase Returns ──────────────────────────────────────────

    def _serialize_return(self, record):
        order = record.purchase_order_id
        supplier = record.partner_id
        lines_data = []
        for line in record.line_ids:
            tmpl = line.product_id.product_tmpl_id
            ol = line.purchase_order_line_id
            lines_data.append(
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                        "brand": getattr(tmpl, "brew_brand", "") or "",
                        "specification": getattr(tmpl, "brew_specification", "") or "",
                        "material_category": getattr(tmpl, "brew_material_category", "") or "",
                    },
                    "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name} if line.lot_id else None,
                    "purchase_order_line_id": str(ol.id) if ol else None,
                    "return_qty": line.return_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "price_unit": line.price_unit,
                    "amount": line.amount,
                    "package_spec": getattr(ol, "brew_package_spec", "") or "",
                    "logistics_no": line.logistics_no or "",
                    "note": line.note or "",
                }
            )
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "purchase_order": {
                "id": str(order.id),
                "name": order.name,
                "state": order.state,
                "date_order": order.date_order.isoformat() if order.date_order else None,
                "currency": order.currency_id.name if order.currency_id else "",
            } if order else None,
            "supplier": {
                "id": str(supplier.id),
                "name": supplier.name,
                "code": getattr(supplier, "brew_supplier_code", "") or "",
            } if supplier else None,
            "receipt_id": str(record.receipt_id.id) if record.receipt_id else None,
            "receipt_name": record.receipt_id.name if record.receipt_id else "",
            "inspection_id": str(record.inspection_id.id) if record.inspection_id else None,
            "approval_instance_id": (
                str(record.approval_instance_id.id)
                if record.approval_instance_id
                else None
            ),
            "approval_flow": self._approval_flow(record.approval_instance_id),
            "approval_history": self._approval_history(record._name, record.id),
            "return_reason": record.return_reason or "",
            "note": record.note or "",
            "logistics_no": record.logistics_no or "",
            "amount_total": record.amount_total,
            "create_date": record.create_date.isoformat() if record.create_date else None,
            "creator": record.create_uid.name if record.create_uid else "",
            "write_date": record.write_date.isoformat() if record.write_date else None,
            "lines": lines_data,
            "version": self._version(record),
        }

    def returns(self, page, page_size, filters=None):
        filters = filters or {}
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("keyword"):
            kw = filters["keyword"]
            domain.append(
                [
                    "|", "|",
                    ("name", "ilike", kw),
                    ("purchase_order_id.name", "ilike", kw),
                    ("line_ids.product_id", "ilike", kw),
                ]
            )
        if filters.get("supplier_name"):
            domain.append(("partner_id.name", "ilike", filters["supplier_name"]))
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("material_category"):
            domain.append(("line_ids.product_id.brew_material_category", "=", filters["material_category"]))
        if filters.get("date_from"):
            domain.append(("create_date", ">=", filters["date_from"] + " 00:00:00"))
        if filters.get("date_to"):
            domain.append(("create_date", "<=", filters["date_to"] + " 23:59:59"))
        model = self.env["brew.purchase.return"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_return(item) for item in records], total

    def get_returnable_lines(self, order_id):
        order = self._record("purchase.order", order_id, factory_field="brew_factory_id")
        receipts = self.env["brew.receipt"].sudo().search(
            [
                ("purchase_order_id", "=", order.id),
                ("state", "in", ["done"]),
                ("company_id", "=", self.company.id),
                ("factory_id", "in", self._factory_ids()),
            ]
        )
        if not receipts:
            return []
        receipt_line_map = {}
        for r in receipts:
            for rl in r.line_ids:
                ol_id = rl.purchase_order_line_id.id
                receipt_line_map.setdefault(ol_id, []).append(rl)
        all_return_lines = self.env["brew.purchase.return.line"].sudo().search(
            [
                ("purchase_order_line_id", "in", list(receipt_line_map.keys())),
                ("return_id.state", "not in", ["cancelled"]),
            ]
        )
        returned_map = {}
        for rl in all_return_lines:
            key = (rl.purchase_order_line_id.id, rl.lot_id.id if rl.lot_id else None)
            returned_map[key] = returned_map.get(key, 0.0) + rl.return_qty
        result = []
        for ol_id, receipt_lines in receipt_line_map.items():
            ol = self.env["purchase.order.line"].sudo().browse(ol_id)
            for rline in receipt_lines:
                rkey = (ol_id, rline.lot_id.id if rline.lot_id else None)
                returned_qty = returned_map.get(rkey, 0.0)
                stock_available_qty = 0.0
                if rline.lot_id:
                    stock_available_qty = self.env["stock.quant"].sudo()._get_available_quantity(
                        rline.product_id,
                        rline.destination_location_id or rline.receipt_id.destination_location_id,
                        lot_id=rline.lot_id,
                        strict=False,
                    )
                returnable_qty = max(
                    min(rline.received_qty - returned_qty, stock_available_qty),
                    0.0,
                )
                if returnable_qty <= 0:
                    continue
                tmpl = rline.product_id.product_tmpl_id
                result.append(
                    {
                        "purchase_order_line_id": str(ol_id),
                        "receipt_line_id": str(rline.id),
                        "receipt_id": str(rline.receipt_id.id),
                        "receipt_name": rline.receipt_id.name,
                        "product": {
                            "id": str(rline.product_id.id),
                            "name": rline.product_id.display_name,
                            "brand": getattr(tmpl, "brew_brand", "") or "",
                            "specification": getattr(tmpl, "brew_specification", "") or "",
                            "material_category": getattr(tmpl, "brew_material_category", "") or "",
                        },
                        "lot": {"id": str(rline.lot_id.id), "name": rline.lot_id.name} if rline.lot_id else None,
                        "received_qty": rline.received_qty,
                        "returned_qty": returned_qty,
                        "stock_available_qty": stock_available_qty,
                        "returnable_qty": returnable_qty,
                        "price_unit": ol.price_unit,
                        "package_spec": getattr(ol, "brew_package_spec", "") or "",
                        "uom": {"id": str(rline.uom_id.id), "name": rline.uom_id.name},
                    }
                )
        return result

    def create_return(self, payload):
        self._require_warehouse()
        order = self._record("purchase.order", payload.get("purchase_order_id"))
        if not order.partner_id:
            raise ApiError("VALIDATION_RELATION", "采购订单缺少供应商信息。", 422)
        receipts = self.env["brew.receipt"].sudo().search(
            [
                ("purchase_order_id", "=", order.id),
                ("state", "=", "done"),
                ("company_id", "=", self.company.id),
            ]
        )
        if not receipts:
            raise ApiError("VALIDATION_RELATION", "该采购订单暂无已入库的收货记录，无法创建退货。", 422)
        factory = self._factory(payload.get("factory_id")) if payload.get("factory_id") else order.brew_factory_id
        if not factory:
            raise ApiError("VALIDATION_REQUIRED", "请选择工厂。", 422)
        receipt = None
        if payload.get("receipt_id"):
            receipt = self._record("brew.receipt", payload["receipt_id"])
        inspection = None
        if payload.get("inspection_id"):
            inspection = self._record("brew.quality.inspection", payload["inspection_id"])
        return_name = (
            self.env["ir.sequence"].sudo().next_by_code("brew.purchase.return") or "New"
        )
        all_return_lines_existing = self.env["brew.purchase.return.line"].sudo().search(
            [
                ("purchase_order_line_id", "in", receipts.line_ids.mapped("purchase_order_line_id").ids),
                ("return_id.state", "not in", ["cancelled"]),
            ]
        )
        returnable_map = {}
        for rline in receipts.line_ids:
            ol_id = rline.purchase_order_line_id.id
            rkey = (ol_id, rline.lot_id.id if rline.lot_id else None)
            stock_available_qty = 0.0
            if rline.lot_id:
                stock_available_qty = self.env["stock.quant"].sudo()._get_available_quantity(
                    rline.product_id,
                    rline.destination_location_id or rline.receipt_id.destination_location_id,
                    lot_id=rline.lot_id,
                    strict=False,
                )
            returnable_map[rkey] = min(
                returnable_map.get(rkey, 0.0) + rline.received_qty,
                stock_available_qty,
            )
        for rl in all_return_lines_existing:
            rkey = (rl.purchase_order_line_id.id, rl.lot_id.id if rl.lot_id else None)
            returnable_map[rkey] = returnable_map.get(rkey, 0.0) - rl.return_qty
        commands = []
        for values in payload.get("lines") or []:
            product = self._record("product.product", values.get("product_id"))
            qty = float(values.get("return_qty") or 0)
            if qty <= 0:
                raise ApiError("VALIDATION_RANGE", "退货数量必须大于零。", 422)
            ol_id = int(values["purchase_order_line_id"]) if values.get("purchase_order_line_id") else None
            lot_id = int(values["lot_id"]) if values.get("lot_id") else None
            if ol_id:
                rkey = (ol_id, lot_id)
                available = returnable_map.get(rkey, 0.0)
                if qty > available + 0.001:
                    raise ApiError("VALIDATION_RANGE", "退货数量超过可退数量（%.3f）。" % available, 422)
                returnable_map[rkey] = returnable_map.get(rkey, 0.0) - qty
            uom = self.env["uom.uom"].sudo().browse(int(values.get("uom_id") or product.uom_id.id))
            ol = None
            if values.get("purchase_order_line_id"):
                ol = self.env["purchase.order.line"].sudo().browse(int(values["purchase_order_line_id"]))
            price = float(values.get("price_unit") or 0)
            if ol and not price:
                price = ol.price_unit
            commands.append(
                Command.create(
                    {
                        "product_id": product.id,
                        "lot_id": int(values["lot_id"]) if values.get("lot_id") else False,
                        "purchase_order_line_id": ol.id if ol else False,
                        "return_qty": qty,
                        "uom_id": uom.id,
                        "price_unit": price,
                        "logistics_no": values.get("logistics_no"),
                        "note": values.get("note"),
                    }
                )
            )
        if not commands:
            raise ApiError("VALIDATION_REQUIRED", "退货明细不能为空。", 422)
        record = self.env["brew.purchase.return"].sudo().create(
            {
                "name": return_name,
                "company_id": self.company.id,
                "factory_id": factory.id,
                "purchase_order_id": order.id,
                "receipt_id": receipt.id if receipt else False,
                "inspection_id": inspection.id if inspection else False,
                "partner_id": order.partner_id.id,
                "return_reason": payload.get("return_reason"),
                "note": payload.get("note"),
                "logistics_no": payload.get("logistics_no"),
                "line_ids": commands,
            }
        )
        return record, self._serialize_return(record)

    def update_return(self, return_id, payload):
        self._require_warehouse()
        record = self._record("brew.purchase.return", return_id)
        if record.state not in ("draft", "returned"):
            raise ApiError("STATE_PRECONDITION_FAILED", "只有草稿或已退回状态的退货单可以编辑。", 422)
        self._check_version(record, payload.get("version"))
        vals = {}
        if "return_reason" in payload:
            vals["return_reason"] = payload["return_reason"]
        if "note" in payload:
            vals["note"] = payload["note"]
        if "logistics_no" in payload:
            vals["logistics_no"] = payload["logistics_no"]
        if "lines" in payload:
            by_id = {line.id: line for line in record.line_ids}
            editable_capacity = {
                (
                    int(line["purchase_order_line_id"]),
                    int(line["lot"]["id"]) if line.get("lot") else None,
                ): line["returnable_qty"]
                for line in self.get_returnable_lines(record.purchase_order_id.id)
            }
            for line in record.line_ids:
                key = (
                    line.purchase_order_line_id.id,
                    line.lot_id.id if line.lot_id else None,
                )
                editable_capacity[key] = editable_capacity.get(key, 0.0) + line.return_qty

            requested_by_source = {}
            for values in payload["lines"]:
                existing = None
                if values.get("id"):
                    existing = by_id.get(int(values["id"]))
                    if not existing:
                        raise ApiError("VALIDATION_RELATION", "退货明细不属于当前退货单。", 422)
                order_line_id = (
                    existing.purchase_order_line_id.id
                    if existing
                    else int(values.get("purchase_order_line_id") or 0)
                )
                lot_id = (
                    existing.lot_id.id
                    if existing and existing.lot_id
                    else int(values.get("lot_id") or 0) or None
                )
                if not order_line_id:
                    raise ApiError("VALIDATION_RELATION", "退货明细必须关联采购订单明细。", 422)
                qty = float(
                    values.get("return_qty")
                    if "return_qty" in values
                    else existing.return_qty
                )
                if qty <= 0:
                    raise ApiError("VALIDATION_RANGE", "退货数量必须大于零。", 422)
                key = (order_line_id, lot_id)
                requested_by_source[key] = requested_by_source.get(key, 0.0) + qty
                available = editable_capacity.get(key, 0.0)
                if requested_by_source[key] > available + 0.001:
                    raise ApiError(
                        "VALIDATION_RANGE",
                        "退货数量超过当前库存可退数量（%.3f）。" % available,
                        422,
                    )

            new_commands = []
            for values in payload["lines"]:
                if values.get("id"):
                    line = by_id.pop(int(values["id"]), None)
                    if line:
                        line.write(
                            {
                                "return_qty": float(values.get("return_qty") or line.return_qty),
                                "logistics_no": values.get("logistics_no", line.logistics_no),
                                "note": values.get("note", line.note),
                            }
                        )
                else:
                    product = self._record("product.product", values.get("product_id"))
                    qty = float(values.get("return_qty") or 0)
                    if qty <= 0:
                        raise ApiError("VALIDATION_RANGE", "退货数量必须大于零。", 422)
                    uom = self.env["uom.uom"].sudo().browse(int(values.get("uom_id") or product.uom_id.id))
                    new_commands.append(
                        Command.create(
                            {
                                "product_id": product.id,
                                "lot_id": int(values["lot_id"]) if values.get("lot_id") else False,
                                "purchase_order_line_id": int(values["purchase_order_line_id"]) if values.get("purchase_order_line_id") else False,
                                "return_qty": qty,
                                "uom_id": uom.id,
                                "price_unit": float(values.get("price_unit") or 0),
                                "logistics_no": values.get("logistics_no"),
                                "note": values.get("note"),
                            }
                        )
                    )
            for leftover in by_id.values():
                leftover.unlink()
            if new_commands:
                record.write({"line_ids": new_commands})
        if vals:
            record.write(vals)
        if payload.get("submit_after_save") is True:
            record.action_submit(self.user)
        return record, self._serialize_return(record)

    def return_action(self, return_id, action, payload):
        self._require_warehouse()
        record = self._record("brew.purchase.return", return_id)
        self._check_version(record, payload.get("version"))
        if action == "submit":
            record.action_submit(self.user)
        elif action == "approve":
            record.action_approve()
        elif action == "return":
            record.action_return(payload.get("reason", ""))
        elif action == "execute":
            record.action_execute()
        elif action == "cancel":
            record.action_cancel()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "退货动作不存在。", 404)
        return record, self._serialize_return(record)
