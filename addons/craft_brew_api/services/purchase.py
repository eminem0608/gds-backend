from datetime import datetime

from odoo import Command, fields

from ..controllers.base import ApiError
from .inventory import InventoryService


PURCHASE_READ_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
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
        if (
            self.user.has_group("craft_brew_core.group_brew_general_manager")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            return self.env["brew.factory"].sudo().search(
                [("company_id", "=", self.company.id), ("active", "=", True)]
            ).ids
        return self.user.brew_factory_ids.filtered(
            lambda item: item.company_id == self.company and item.active
        ).ids

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
        if step.role_code not in self.user.brew_role_codes():
            raise ApiError('PERMISSION_DENIED', 'You cannot process this approval step.', 403)
        return step

    def _retire_duplicate_approved_instances(self, target_model, target_id, current_id):
        stale = self.env["brew.approval.instance"].sudo().search([
            ("company_id", "=", self.company.id),
            ("target_model", "=", target_model),
            ("target_id", "=", target_id),
            ("state", "=", "approved"),
            ("id", "!=", current_id),
        ])
        if stale:
            stale.write({"state": "cancelled", "completed_at": fields.Datetime.now()})

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
        return product

    def _uom(self, uom_id, product):
        uom = self.env["uom.uom"].sudo().browse(int(uom_id or 0)).exists()
        if not uom or uom.category_id != product.uom_id.category_id:
            raise ApiError("VALIDATION_RELATION", "采购单位与物料不一致。", 422)
        return uom

    def _serialize_requisition(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "request_type": record.request_type,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "need_date": record.need_date.isoformat(),
            "reason": record.reason,
            "emergency_reason": record.emergency_reason,
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
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "requested_qty": line.requested_qty,
                    "approved_qty": line.approved_qty,
                    "ordered_qty": line.ordered_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "allocations": [
                        {
                            "plan_id": str(allocation.production_plan_id.id),
                            "plan_name": allocation.production_plan_id.name,
                            "required_qty": allocation.required_qty,
                            "shortage_qty": allocation.shortage_qty,
                        }
                        for allocation in line.allocation_ids
                    ],
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def requisitions(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.purchase.requisition"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="need_date,id",
        )
        return [self._serialize_requisition(item) for item in records], total

    def requisition_source_plans(self):
        self._require_purchase()
        plans = self.env["brew.production.plan"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("factory_id", "in", self._factory_ids()),
                ("state", "in", ["approved", "dispatched", "in_progress"]),
            ],
            order="planned_start,id",
            limit=100,
        )
        inventory = InventoryService(self.env, self.user, self.company)
        result = []
        for plan in plans:
            shortage = inventory.plan_inventory(plan.id, reserve=False)
            materials = [
                item for item in shortage["materials"] if item["shortage"] > 0
            ]
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
                    "materials": materials,
                    "shortage_items": len(materials),
                }
            )
        return result

    def create_requisition(self, payload):
        self._require_purchase()
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
                "requester_id": self.user.id,
                "line_ids": commands,
            }
        )
        return record, self._serialize_requisition(record)

    def aggregate_requisitions(self, payload):
        self._require_purchase()
        plan_ids = list(dict.fromkeys(int(value) for value in payload.get("plan_ids") or []))
        if not plan_ids or not payload.get("need_date"):
            raise ApiError("VALIDATION_REQUIRED", "生产计划和需求日期不能为空。", 422)
        inventory = InventoryService(self.env, self.user, self.company)
        grouped = {}
        for plan_id in plan_ids:
            plan = inventory._plan(plan_id)
            result = inventory.plan_inventory(plan.id, reserve=False)
            factory_group = grouped.setdefault(plan.factory_id.id, {})
            for item in result["materials"]:
                if item["shortage"] <= 0:
                    continue
                product_id = int(item["product"]["id"])
                values = factory_group.setdefault(
                    product_id,
                    {"quantity": 0.0, "allocations": [], "uom": item["uom"]},
                )
                values["quantity"] += item["shortage"]
                values["allocations"].append(
                    {
                        "plan": plan,
                        "required": item["required"],
                        "shortage": item["shortage"],
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
        return records, [self._serialize_requisition(item) for item in records]

    def requisition_action(self, record_id, action, payload):
        record = self._record("brew.purchase.requisition", record_id)
        self._check_version(record, payload.get("version"))
        if action == "submit":
            self._require_purchase()
            record.action_submit(self.user)
        elif action == "approve":
            self._require_director()
            step = self._pending_approval_step(record.approval_instance_id)
            self._retire_duplicate_approved_instances(record._name, record.id, record.approval_instance_id.id)
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

    def _serialize_order(self, record):
        return {
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
            "supplier": {"id": str(record.partner_id.id), "name": record.partner_id.name},
            "order_date": record.date_order.isoformat() if record.date_order else None,
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
            "approval_instance_id": (
                str(record.brew_approval_instance_id.id)
                if record.brew_approval_instance_id
                else None
            ),
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "quantity": line.product_qty,
                    "uom": {
                        "id": str(line.product_uom.id),
                        "name": line.product_uom.name,
                    },
                    "price_unit": line.price_unit,
                    "subtotal": line.price_subtotal,
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

    def orders(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("brew_factory_id", "in", self._factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["purchase.order"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="date_order desc,id desc",
        )
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
            "date_planned": values.get("planned_date")
            or fields.Datetime.to_string(
                fields.Datetime.now()
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
                "brew_factory_id": factory.id,
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
        line_requests = payload.get("lines") or []
        if not line_requests:
            raise ApiError("VALIDATION_REQUIRED", "请选择采购需求明细。", 422)
        normalized = []
        requisition_ids = set()
        requisition_types = set()
        factory_id = None
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
            requisition_ids.add(line.requisition_id.id)
            requisition_types.add(line.requisition_id.request_type)
            if len(requisition_types) > 1:
                raise ApiError(
                    "VALIDATION_RELATION", "计划采购与紧急采购不能合并到同一订单。", 422
                )
            factory_id = factory_id or line.requisition_id.factory_id.id
            if factory_id != line.requisition_id.factory_id.id:
                raise ApiError("VALIDATION_RELATION", "一张采购订单只能对应一个厂区。", 422)
            quantity = float(
                values.get("quantity")
                or line.approved_qty - line.ordered_qty
            )
            normalized.append(
                {
                    "product_id": line.product_id.id,
                    "uom_id": line.uom_id.id,
                    "quantity": quantity,
                    "price_unit": values.get("price_unit", line.product_id.standard_price),
                    "planned_date": values.get("planned_date"),
                    "requisition_line_ids": [line.id],
                }
            )
        return self.create_order(
            {
                "factory_id": factory_id,
                "supplier_id": payload.get("supplier_id"),
                "requisition_ids": list(requisition_ids),
                "lines": normalized,
            }
        )

    def update_order(self, order_id, payload):
        self._require_purchase()
        order = self._record("purchase.order", order_id, "brew_factory_id")
        self._check_version(order, payload.get("version"))
        if order.state not in {"draft", "sent"} or order.brew_approval_state == "pending":
            raise ApiError("STATE_PRECONDITION_FAILED", "当前采购订单不可修改。", 422)
        values = {}
        if "supplier_id" in payload:
            values["partner_id"] = self._supplier(payload["supplier_id"]).id
        if "order_date" in payload:
            values["date_order"] = payload["order_date"]
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的采购订单字段。", 422)
        order.write(values)
        return order, self._serialize_order(order)

    def order_action(self, order_id, action, payload):
        order = self._record("purchase.order", order_id, "brew_factory_id")
        self._check_version(order, payload.get("version"))
        if action == "submit":
            self._require_purchase()
            order.action_brew_submit(self.user)
        elif action == "approve":
            self._require_director()
            step = self._pending_approval_step(order.brew_approval_instance_id)
            self._retire_duplicate_approved_instances(order._name, order.id, order.brew_approval_instance_id.id)
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

    def _serialize_receipt(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "purchase_order": {
                "id": str(record.purchase_order_id.id),
                "name": record.purchase_order_id.name,
            },
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
            "quality_inspection_id": (
                str(record.quality_inspection_id.id)
                if record.quality_inspection_id
                else None
            ),
            "arrived_at": record.arrived_at.isoformat() if record.arrived_at else None,
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at else None
            ),
            "lines": [
                {
                    "id": str(line.id),
                    "order_line_id": str(line.purchase_order_line_id.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "received_qty": line.received_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "lot_name": line.lot_name,
                    "supplier_lot": line.supplier_lot,
                    "expiration_date": (
                        line.expiration_date.isoformat()
                        if line.expiration_date
                        else None
                    ),
                    "lot_id": str(line.lot_id.id) if line.lot_id else None,
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def receipts(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
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
        destination = self._location(
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
        commands = []
        for values in payload.get("lines") or []:
            order_line = order.order_line.filtered(
                lambda item: item.id == int(values.get("order_line_id") or 0)
            )
            quantity = float(values.get("received_qty") or 0)
            lot_name = str(values.get("lot_name") or "").strip()
            if not order_line or quantity <= 0 or not lot_name:
                raise ApiError("VALIDATION_REQUIRED", "收货明细、数量和批次号不能为空。", 422)
            commands.append(
                Command.create(
                    {
                        "purchase_order_line_id": order_line.id,
                        "received_qty": quantity,
                        "lot_name": lot_name,
                        "supplier_lot": values.get("supplier_lot"),
                        "expiration_date": values.get("expiration_date"),
                    }
                )
            )
        if not commands:
            raise ApiError("VALIDATION_REQUIRED", "收货明细不能为空。", 422)
        receipt = self.env["brew.receipt"].sudo().create(
            {
                "purchase_order_id": order.id,
                "shipment_id": shipment.id or False,
                "qc_location_id": qc_location.id,
                "destination_location_id": destination.id,
                "line_ids": commands,
            }
        )
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
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "receipt_id": str(record.receipt_id.id),
            "receipt_name": record.receipt_id.name,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "inspector": (
                {"id": str(record.inspector_id.id), "name": record.inspector_id.name}
                if record.inspector_id
                else None
            ),
            "inspected_at": (
                record.inspected_at.isoformat() if record.inspected_at else None
            ),
            "note": record.note,
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name},
                    "inspected_qty": line.inspected_qty,
                    "qualified_qty": line.qualified_qty,
                    "rejected_qty": line.rejected_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "disposition": line.disposition,
                    "result": line.result_json,
                    "note": line.note,
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def inspections(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
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
        if inspection.state != "draft":
            raise ApiError("STATE_PRECONDITION_FAILED", "已完成质检不可修改。", 422)
        by_id = {line.id: line for line in inspection.line_ids}
        incoming = payload.get("lines") or []
        if not incoming:
            raise ApiError("VALIDATION_REQUIRED", "质检结果不能为空。", 422)
        for values in incoming:
            line = by_id.get(int(values.get("id") or 0))
            if not line:
                raise ApiError("VALIDATION_RELATION", "质检明细不存在。", 422)
            qualified = float(values.get("qualified_qty") or 0)
            rejected = float(values.get("rejected_qty") or 0)
            if qualified < 0 or rejected < 0:
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
        return inspection, self._serialize_inspection(inspection)

    def inspection_action(self, inspection_id, action, payload):
        self._require_warehouse()
        inspection = self._record("brew.quality.inspection", inspection_id)
        self._check_version(inspection, payload.get("version"))
        if action == "pass" and any(inspection.line_ids.mapped("rejected_qty")):
            raise ApiError("STATE_PRECONDITION_FAILED", "存在不合格数量时不能执行全部合格。", 422)
        if action not in {"pass", "reject"}:
            raise ApiError("RESOURCE_NOT_FOUND", "质检动作不存在。", 404)
        if action == "reject" and not any(inspection.line_ids.mapped("rejected_qty")):
            raise ApiError("STATE_PRECONDITION_FAILED", "未录入不合格数量。", 422)
        inspection.action_complete(self.user)
        return inspection, self._serialize_inspection(inspection)
