import json

from odoo import Command, fields

from ..controllers.base import ApiError
from .inventory import InventoryService


PRODUCTION_READ_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_operator",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_admin",
}


class ProductionService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not any(user.has_group(group) for group in PRODUCTION_READ_GROUPS):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看生产执行数据。", 403)
        self.inventory = InventoryService(env, user, company, enforce_access=False)

    def _require_warehouse(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_warehouse")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有库管或系统管理员可以执行该操作。", 403)

    def _require_operator(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_operator")
            or self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权执行现场生产操作。", 403)

    def _require_director(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_general_manager")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权审核生产记录。", 403)

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

    def _record(self, model, record_id):
        record = self.env[model].sudo().search(
            [
                ("id", "=", int(record_id or 0)),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "生产业务单据不存在。", 404)
        factory = record.factory_id
        if factory and factory.id not in self.inventory._allowed_factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "生产业务单据不在当前账号范围内。", 403)
        return record

    def _serialize_requisition(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "plan": {
                "id": str(record.production_plan_id.id),
                "name": record.production_plan_id.name,
            },
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "source_location": {
                "id": str(record.source_location_id.id),
                "name": record.source_location_id.complete_name,
            },
            "production_location": {
                "id": str(record.production_location_id.id),
                "name": record.production_location_id.complete_name,
            },
            "overage_reason": record.overage_reason,
            "approval_instance_id": (
                str(record.approval_instance_id.id)
                if record.approval_instance_id
                else None
            ),
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "planned_qty": line.planned_qty,
                    "requested_qty": line.requested_qty,
                    "issued_qty": line.issued_qty,
                    "acknowledged_qty": line.acknowledged_qty,
                    "returned_qty": line.returned_qty,
                    "consumed_qty": line.consumed_qty,
                    "loss_qty": line.loss_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                    "allocations": [
                        {
                            "id": str(item.id),
                            "lot": {"id": str(item.lot_id.id), "name": item.lot_id.name},
                            "location": {
                                "id": str(item.location_id.id),
                                "name": item.location_id.complete_name,
                            },
                            "recommended_qty": item.recommended_qty,
                            "issued_qty": item.issued_qty,
                            "acknowledged_qty": item.acknowledged_qty,
                            "returned_qty": item.returned_qty,
                            "consumed_qty": item.consumed_qty,
                            "loss_qty": item.loss_qty,
                        }
                        for item in line.allocation_ids
                    ],
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def requisitions(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self.inventory._allowed_factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.material.requisition"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_requisition(item) for item in records], total

    def create_requisition(self, payload):
        self._require_warehouse()
        plan = self.inventory._plan(payload.get("production_plan_id"))
        if plan.state not in {"approved", "dispatched", "in_progress"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "生产计划尚未批准。", 422)
        source = self.inventory._location(payload.get("source_location_id"))
        destination = self.inventory._location(payload.get("production_location_id"))
        if source.brew_factory_id != plan.factory_id or destination.brew_factory_id != plan.factory_id:
            raise ApiError("VALIDATION_RELATION", "领料库位必须属于生产计划厂区。", 422)
        materials = json.loads(plan.material_snapshot_json or "{}").get("materials", [])
        if not materials:
            materials = plan.expand_materials().get("materials", [])
        overrides = {
            int(item["product_id"]): float(item["requested_qty"])
            for item in payload.get("lines") or []
            if item.get("product_id")
        }
        commands = []
        for material in materials:
            product = self.inventory._product(material["material_id"])
            uom = self.env["uom.uom"].sudo().browse(int(material["uom_id"])).exists()
            planned = float(material["quantity"])
            commands.append(
                Command.create(
                    {
                        "product_id": product.id,
                        "planned_qty": planned,
                        "requested_qty": overrides.get(product.id, planned),
                        "uom_id": uom.id,
                    }
                )
            )
        record = self.env["brew.material.requisition"].sudo().create(
            {
                "production_plan_id": plan.id,
                "source_location_id": source.id,
                "production_location_id": destination.id,
                "line_ids": commands,
            }
        )
        return record, self._serialize_requisition(record)

    def recommend_lots(self, record_id, payload):
        self._require_warehouse()
        record = self._record("brew.material.requisition", record_id)
        self._check_version(record, payload.get("version"))
        if record.state not in {"draft", "overage_pending"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "当前领料单不能推荐批次。", 422)
        if (
            record.overage_metrics()["quantity"] > 0
            and (
                not record.approval_instance_id
                or record.approval_instance_id.state != "approved"
            )
        ):
            raise ApiError("APPROVAL_REQUIRED", "超领审批通过后才能推荐批次。", 409)
        old_reservations = self.env["brew.inventory.reservation"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("state", "=", "active"),
                "|",
                "&",
                ("source_model", "=", record.production_plan_id._name),
                ("source_id", "=", record.production_plan_id.id),
                "&",
                ("source_model", "=", record._name),
                ("source_id", "=", record.id),
            ]
        )
        old_reservations.action_release()
        record.line_ids.mapped("allocation_ids").unlink()
        reservation_values = []
        shortages = []
        for line in record.line_ids:
            needed_base = line.uom_id._compute_quantity(
                line.requested_qty, line.product_id.uom_id
            )
            remaining = needed_base
            allocations = []
            for candidate in self.inventory._candidate_stock(
                line.product_id, record.factory_id, lock=True
            ):
                take = min(candidate["quantity"], remaining)
                if take <= 0:
                    continue
                display_qty = line.product_id.uom_id._compute_quantity(take, line.uom_id)
                allocations.append(
                    Command.create(
                        {
                            "lot_id": candidate["lot"].id,
                            "location_id": candidate["location"].id,
                            "recommended_qty": display_qty,
                        }
                    )
                )
                reservation_values.append(
                    {
                        "company_id": self.company.id,
                        "factory_id": record.factory_id.id,
                        "product_id": line.product_id.id,
                        "lot_id": candidate["lot"].id,
                        "location_id": candidate["location"].id,
                        "quantity": take,
                        "uom_id": line.product_id.uom_id.id,
                        "source_model": record._name,
                        "source_id": record.id,
                        "expires_at": payload.get("expires_at"),
                    }
                )
                remaining -= take
                if remaining <= line.product_id.uom_id.rounding:
                    remaining = 0
                    break
            if remaining:
                shortages.append(
                    {
                        "product_id": str(line.product_id.id),
                        "product_name": line.product_id.display_name,
                        "shortage": remaining,
                        "uom": line.product_id.uom_id.name,
                    }
                )
            line.write({"allocation_ids": allocations})
        if shortages:
            raise ApiError(
                "INVENTORY_INSUFFICIENT",
                "可用库存不足，未生成领料批次推荐。",
                409,
                {"shortages": shortages},
            )
        self.env["brew.inventory.reservation"].sudo().create(reservation_values)
        record.action_mark_recommended()
        return record, self._serialize_requisition(record)

    def requisition_action(self, record_id, action, payload):
        record = self._record("brew.material.requisition", record_id)
        self._check_version(record, payload.get("version"))
        if action == "request-overage":
            self._require_operator()
            record.action_request_overage(self.user, payload.get("reason"))
        elif action == "issue":
            self._require_warehouse()
            record.action_issue()
        elif action == "acknowledge":
            self._require_operator()
            values_by_id = {
                int(item.get("allocation_id") or 0): item
                for item in payload.get("allocations") or []
            }
            for allocation in record.line_ids.mapped("allocation_ids"):
                values = values_by_id.get(allocation.id, {})
                allocation.acknowledged_qty = float(
                    values.get("acknowledged_qty", allocation.issued_qty)
                )
            record.action_acknowledge()
        elif action == "return":
            self._require_operator()
            record.action_return_materials(payload.get("allocations") or [])
        elif action == "complete":
            self._require_operator()
            values_by_id = {
                int(item.get("allocation_id") or 0): item
                for item in payload.get("allocations") or []
            }
            for allocation in record.line_ids.mapped("allocation_ids"):
                values = values_by_id.get(allocation.id)
                remaining = allocation.acknowledged_qty - allocation.returned_qty
                allocation.write(
                    {
                        "consumed_qty": float(
                            values.get("consumed_qty", remaining) if values else remaining
                        ),
                        "loss_qty": float(values.get("loss_qty", 0) if values else 0),
                    }
                )
            record.action_complete()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "领料动作不存在。", 404)
        return record, self._serialize_requisition(record)

    def _serialize_batch(self, record, detail=False):
        data = {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "plan": {
                "id": str(record.production_plan_id.id),
                "name": record.production_plan_id.name,
            },
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "product": {
                "id": str(record.product_tmpl_id.id),
                "name": record.product_tmpl_id.display_name,
            },
            "tank": {"id": str(record.tank_id.id), "name": record.tank_id.name},
            "planned_volume_l": record.planned_volume_l,
            "actual_volume_l": record.actual_volume_l,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "entered_tank_at": (
                record.entered_tank_at.isoformat() if record.entered_tank_at else None
            ),
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at else None
            ),
            "version": self._version(record),
        }
        if detail:
            data.update(
                {
                    "recipe_snapshot": json.loads(record.recipe_snapshot_json or "{}"),
                    "material_snapshot": json.loads(record.material_snapshot_json or "{}"),
                    "records": [
                        self._serialize_record(item)
                        for item in record.record_ids.sorted("recorded_at")
                    ],
                    "actual_materials": [
                        {
                            "product_name": item.product_id.display_name,
                            "lot_name": item.lot_id.name,
                            "quantity": item.quantity,
                            "uom": item.uom_id.name,
                            "action_type": item.action_type,
                        }
                        for item in record.material_actual_ids
                    ],
                }
            )
        return data

    def batches(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self.inventory._allowed_factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.production.batch"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_batch(item) for item in records], total

    def create_batch(self, payload):
        self._require_director()
        plan = self.inventory._plan(payload.get("production_plan_id"))
        if plan.state != "dispatched":
            raise ApiError("STATE_PRECONDITION_FAILED", "只有已下发计划可创建生产批次。", 422)
        batch = self.env["brew.production.batch"].sudo().create(
            {"production_plan_id": plan.id}
        )
        return batch, self._serialize_batch(batch, True)

    def batch_detail(self, batch_id):
        return self._serialize_batch(
            self._record("brew.production.batch", batch_id), True
        )

    def batch_action(self, batch_id, action, payload):
        batch = self._record("brew.production.batch", batch_id)
        self._check_version(batch, payload.get("version"))
        self._require_operator()
        if action == "start":
            batch.action_start()
        elif action == "enter-tank":
            batch.action_enter_tank(payload.get("actual_volume_l"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "生产批次动作不存在。", 404)
        return batch, self._serialize_batch(batch, True)

    def batch_timeline(self, batch_id):
        batch = self._record("brew.production.batch", batch_id)
        items = []
        if batch.started_at:
            items.append(
                {"at": batch.started_at.isoformat(), "type": "batch_started", "title": "生产开始"}
            )
        for record in batch.record_ids:
            items.append(
                {
                    "at": record.recorded_at.isoformat(),
                    "type": f"record.{record.record_type}",
                    "title": dict(record._fields["record_type"].selection).get(
                        record.record_type, record.record_type
                    ),
                    "state": record.state,
                    "record_id": str(record.id),
                }
            )
        if batch.entered_tank_at:
            items.append(
                {
                    "at": batch.entered_tank_at.isoformat(),
                    "type": "entered_tank",
                    "title": f"进罐 {batch.tank_id.name}",
                }
            )
        return sorted(items, key=lambda item: item["at"])

    def _serialize_record(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "batch_id": str(record.batch_id.id),
            "batch_name": record.batch_id.name,
            "record_type": record.record_type,
            "state": record.state,
            "version_number": record.version,
            "corrects_record_id": (
                str(record.corrects_record_id.id) if record.corrects_record_id else None
            ),
            "correction_reason": record.correction_reason,
            "equipment": (
                {"id": str(record.equipment_id.id), "name": record.equipment_id.name}
                if record.equipment_id
                else None
            ),
            "operator": {
                "id": str(record.operator_id.id),
                "name": record.operator_id.name,
            },
            "recorded_at": record.recorded_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "ended_at": record.ended_at.isoformat() if record.ended_at else None,
            "parameters": json.loads(record.parameters_json or "{}"),
            "note": record.note,
            "attachments": [
                {"id": str(item.id), "name": item.name} for item in record.attachment_ids
            ],
            "materials": [
                {
                    "id": str(item.id),
                    "product": {
                        "id": str(item.product_id.id),
                        "name": item.product_id.display_name,
                    },
                    "lot": {"id": str(item.lot_id.id), "name": item.lot_id.name},
                    "quantity": item.quantity,
                    "uom": {"id": str(item.uom_id.id), "name": item.uom_id.name},
                    "action_type": item.action_type,
                }
                for item in record.material_actual_ids
            ],
            "return_reason": record.return_reason,
            "version": self._version(record),
        }

    def records(self, page, page_size, filters):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self.inventory._allowed_factory_ids()),
        ]
        if filters.get("batch_id"):
            domain.append(("batch_id", "=", int(filters["batch_id"])))
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("record_type"):
            domain.append(("record_type", "=", filters["record_type"]))
        model = self.env["brew.production.record"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="recorded_at desc,id desc",
        )
        return [self._serialize_record(item) for item in records], total

    def _material_commands(self, batch, materials):
        commands = []
        for values in materials or []:
            product = self.inventory._product(values.get("product_id"))
            lot = self.inventory._lot(values.get("lot_id"))
            uom = self.env["uom.uom"].sudo().browse(int(values.get("uom_id") or 0)).exists()
            quantity = float(values.get("quantity") or 0)
            if lot.product_id != product or not uom or uom.category_id != product.uom_id.category_id:
                raise ApiError("VALIDATION_RELATION", "实际物料、批次或单位不一致。", 422)
            if quantity <= 0:
                raise ApiError("VALIDATION_RANGE", "实际物料数量必须大于零。", 422)
            commands.append(
                Command.create(
                    {
                        "batch_id": batch.id,
                        "product_id": product.id,
                        "lot_id": lot.id,
                        "quantity": quantity,
                        "uom_id": uom.id,
                        "action_type": values.get("action_type", "consumed"),
                    }
                )
            )
        return commands

    def create_record(self, payload):
        self._require_operator()
        batch = self._record("brew.production.batch", payload.get("batch_id"))
        if batch.state not in {"in_progress", "in_tank"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "生产批次尚未开始或已结束。", 422)
        if payload.get("record_type") not in {
            "cleaning",
            "mashing",
            "lautering",
            "boiling",
            "cooling",
            "yeast",
            "other",
        }:
            raise ApiError("VALIDATION_SELECTION", "生产记录类型无效。", 422)
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ApiError("VALIDATION_FORMAT", "生产参数必须是对象。", 422)
        record = self.env["brew.production.record"].sudo().create(
            {
                "batch_id": batch.id,
                "record_type": payload.get("record_type"),
                "equipment_id": int(payload["equipment_id"]) if payload.get("equipment_id") else False,
                "operator_id": self.user.id,
                "recorded_at": payload.get("recorded_at") or fields.Datetime.now(),
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "parameters_json": json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                "note": payload.get("note"),
                "attachment_ids": [Command.set([int(item) for item in payload.get("attachment_ids") or []])],
                "material_actual_ids": self._material_commands(batch, payload.get("materials")),
            }
        )
        return record, self._serialize_record(record)

    def update_record(self, record_id, payload):
        self._require_operator()
        record = self._record("brew.production.record", record_id)
        self._check_version(record, payload.get("version"))
        values = {}
        for source, target in {
            "recorded_at": "recorded_at",
            "started_at": "started_at",
            "ended_at": "ended_at",
            "note": "note",
        }.items():
            if source in payload:
                values[target] = payload[source]
        if "parameters" in payload:
            if not isinstance(payload["parameters"], dict):
                raise ApiError("VALIDATION_FORMAT", "生产参数必须是对象。", 422)
            values["parameters_json"] = json.dumps(
                payload["parameters"], ensure_ascii=False, sort_keys=True
            )
        if "attachment_ids" in payload:
            values["attachment_ids"] = [
                Command.set([int(item) for item in payload["attachment_ids"]])
            ]
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的生产记录字段。", 422)
        record.write(values)
        return record, self._serialize_record(record)

    def record_action(self, record_id, action, payload):
        record = self._record("brew.production.record", record_id)
        self._check_version(record, payload.get("version"))
        if action == "submit":
            self._require_operator()
            record.action_submit()
        elif action == "approve":
            self._require_director()
            record.action_approve(self.user)
        elif action == "return":
            self._require_director()
            record.action_return(payload.get("reason"))
        elif action == "correct":
            self._require_director()
            correction = record.create_correction(payload.get("reason"))
            return correction, self._serialize_record(correction)
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "生产记录动作不存在。", 404)
        return record, self._serialize_record(record)
