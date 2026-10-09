import json
from datetime import date

from odoo import Command, fields
from odoo.exceptions import ValidationError

from ..controllers.base import ApiError


READ_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_purchase",
    "craft_brew_core.group_brew_sales",
    "craft_brew_core.group_brew_finance",
    "craft_brew_core.group_brew_admin",
}


class InventoryService:
    def __init__(self, env, user, company, enforce_access=True):
        self.env = env
        self.user = user
        self.company = company
        if enforce_access and not any(user.has_group(group) for group in READ_GROUPS):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看库存。", 403)

    def _require_manage(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_warehouse")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有库管或系统管理员可以执行库存操作。", 403)

    def _allowed_factory_ids(self):
        if (
            self.user.has_group("craft_brew_core.group_brew_general_manager")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            return self.env["brew.factory"].sudo().search(
                [("company_id", "=", self.company.id), ("active", "=", True)]
            ).ids
        return self.user.brew_factory_ids.filtered(
            lambda factory: factory.company_id == self.company and factory.active
        ).ids

    def _factory(self, factory_id):
        factory = self.env["brew.factory"].sudo().search(
            [
                ("id", "=", int(factory_id or 0)),
                ("company_id", "=", self.company.id),
                ("id", "in", self._allowed_factory_ids()),
                ("active", "=", True),
            ],
            limit=1,
        )
        if not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区不存在或不在当前账号数据范围内。", 403)
        return factory

    def _location(self, location_id, internal=True):
        domain = [
            ("id", "=", int(location_id or 0)),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company.id),
        ]
        if internal:
            domain.append(("usage", "=", "internal"))
        location = self.env["stock.location"].sudo().search(domain, limit=1)
        if (
            not location
            or (
                internal
                and location.brew_factory_id
                and location.brew_factory_id.id not in self._allowed_factory_ids()
            )
        ):
            raise ApiError("DATA_SCOPE_DENIED", "库位不存在或不在当前账号数据范围内。", 403)
        return location

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
            raise ApiError("RESOURCE_NOT_FOUND", "产品或物料不存在。", 404)
        return product

    def _lot(self, lot_id):
        lot = self.env["stock.lot"].sudo().search(
            [
                ("id", "=", int(lot_id or 0)),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not lot:
            raise ApiError("RESOURCE_NOT_FOUND", "库存批次不存在。", 404)
        return lot

    def _location_domain(self, factory_id=None, location_id=None):
        domain = [
            ("usage", "=", "internal"),
            ("company_id", "=", self.company.id),
            ("brew_factory_id", "in", self._allowed_factory_ids()),
        ]
        if factory_id:
            factory = self._factory(factory_id)
            domain.append(("brew_factory_id", "=", factory.id))
        if location_id:
            location = self._location(location_id)
            domain.append(("id", "child_of", location.id))
        return domain

    def _reservation_totals(
        self, location_ids=None, product_id=None, lot_id=None, excluded_source=None
    ):
        domain = [
            ("company_id", "=", self.company.id),
            ("state", "=", "active"),
        ]
        if location_ids:
            domain.append(("location_id", "in", location_ids))
        if product_id:
            domain.append(("product_id", "=", product_id))
        if lot_id:
            domain.append(("lot_id", "=", lot_id))
        if excluded_source:
            domain += [
                "|",
                ("source_model", "!=", excluded_source[0]),
                ("source_id", "!=", excluded_source[1]),
            ]
        totals = {}
        for item in self.env["brew.inventory.reservation"].sudo().search(domain):
            key = (item.location_id.id, item.product_id.id, item.lot_id.id)
            totals[key] = totals.get(key, 0) + item.quantity_base
        return totals

    @staticmethod
    def _category(product):
        return product.product_tmpl_id.brew_item_type or "other"

    def summary(self, factory_id=None, location_id=None):
        locations = self.env["stock.location"].sudo().search(
            self._location_domain(factory_id, location_id)
        )
        quants = self.env["stock.quant"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("location_id", "in", locations.ids),
                ("quantity", "!=", 0),
            ]
        )
        reservation_totals = self._reservation_totals(locations.ids)
        categories = {}

        def bucket(product):
            code = self._category(product)
            return categories.setdefault(
                code,
                {
                    "category": code,
                    "on_hand": 0.0,
                    "reserved": 0.0,
                    "available": 0.0,
                    "pending_quality": 0.0,
                    "incoming": 0.0,
                    "frozen": 0.0,
                    "uom_note": "按产品基础单位汇总，仅同类下钻可直接比较",
                },
            )

        today = fields.Date.today()
        for quant in quants:
            values = bucket(quant.product_id)
            quantity = quant.quantity
            custom_reserved = reservation_totals.get(
                (quant.location_id.id, quant.product_id.id, quant.lot_id.id), 0
            )
            reserved = max(quant.reserved_quantity, 0) + custom_reserved
            values["on_hand"] += quantity
            values["reserved"] += min(max(quantity, 0), reserved)
            lot = quant.lot_id
            if lot and lot.brew_quality_status == "pending":
                values["pending_quality"] += max(quantity, 0)
            if lot and lot.brew_frozen:
                values["frozen"] += max(quantity, 0)
            usable = (
                quantity > 0
                and quant.location_id.brew_location_type != "qc"
                and (not lot or lot.brew_quality_status == "qualified")
                and (not lot or not lot.brew_frozen)
                and (
                    not lot
                    or not lot.brew_expiration_date
                    or lot.brew_expiration_date >= today
                )
            )
            if usable:
                values["available"] += max(quantity - reserved, 0)

        incoming_moves = self.env["stock.move"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("state", "in", ["confirmed", "waiting", "assigned", "partially_available"]),
                ("location_dest_id", "in", locations.ids),
                ("location_id.usage", "!=", "internal"),
            ]
        )
        for move in incoming_moves:
            bucket(move.product_id)["incoming"] += move.product_uom._compute_quantity(
                move.product_uom_qty, move.product_id.uom_id
            )
        totals = {
            key: sum(item[key] for item in categories.values())
            for key in [
                "on_hand",
                "reserved",
                "available",
                "pending_quality",
                "incoming",
                "frozen",
            ]
        }
        return {"totals": totals, "categories": list(categories.values())}

    def _serialize_lot(self, lot, quants=None):
        if quants is None:
            locations = self.env["stock.location"].sudo().search(self._location_domain())
            quants = self.env["stock.quant"].sudo().search(
                [
                    ("company_id", "=", self.company.id),
                    ("lot_id", "=", lot.id),
                    ("location_id", "in", locations.ids),
                ]
            )
        reservations = self._reservation_totals(
            quants.mapped("location_id").ids, lot.product_id.id, lot.id
        )
        on_hand = sum(quants.mapped("quantity"))
        reserved = sum(quants.mapped("reserved_quantity")) + sum(reservations.values())
        return {
            "id": str(lot.id),
            "name": lot.name,
            "supplier_lot": lot.brew_supplier_lot,
            "product": {
                "id": str(lot.product_id.id),
                "name": lot.product_id.display_name,
                "category": self._category(lot.product_id),
            },
            "expiration_date": (
                lot.brew_expiration_date.isoformat() if lot.brew_expiration_date else None
            ),
            "quality_status": lot.brew_quality_status,
            "frozen": lot.brew_frozen,
            "freeze_reason": lot.brew_freeze_reason,
            "on_hand": on_hand,
            "reserved": reserved,
            "available": max(on_hand - reserved, 0)
            if lot.brew_quality_status == "qualified"
            and not lot.brew_frozen
            and (
                not lot.brew_expiration_date
                or lot.brew_expiration_date >= fields.Date.today()
            )
            else 0,
            "uom": lot.product_id.uom_id.name,
            "locations": [
                {
                    "id": str(quant.location_id.id),
                    "name": quant.location_id.complete_name,
                    "quantity": quant.quantity,
                    "reserved": quant.reserved_quantity
                    + reservations.get(
                        (quant.location_id.id, quant.product_id.id, lot.id), 0
                    ),
                    "received_at": quant.in_date.isoformat() if quant.in_date else None,
                }
                for quant in quants
                if quant.quantity
            ],
        }

    def lots(self, page, page_size, filters):
        locations = self.env["stock.location"].sudo().search(
            self._location_domain(filters.get("factory_id"), filters.get("location_id"))
        )
        quant_domain = [
            ("company_id", "=", self.company.id),
            ("location_id", "in", locations.ids),
            ("quantity", "!=", 0),
            ("lot_id", "!=", False),
        ]
        if filters.get("product_id"):
            quant_domain.append(("product_id", "=", int(filters["product_id"])))
        lot_ids = list(dict.fromkeys(
            self.env["stock.quant"].sudo().search(quant_domain).mapped("lot_id").ids
        ))
        domain = [("id", "in", lot_ids)]
        if filters.get("quality_status"):
            domain.append(("brew_quality_status", "=", filters["quality_status"]))
        if filters.get("frozen") in {"true", "false"}:
            domain.append(("brew_frozen", "=", filters["frozen"] == "true"))
        model = self.env["stock.lot"].sudo()
        total = model.search_count(domain)
        lots = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="brew_expiration_date asc, id asc",
        )
        quants = self.env["stock.quant"].sudo().search(
            quant_domain + [("lot_id", "in", lots.ids)]
        )
        return [
            self._serialize_lot(lot, quants.filtered(lambda quant: quant.lot_id == lot))
            for lot in lots
        ], total

    def lot_detail(self, lot_id):
        lot = self._lot(lot_id)
        data = self._serialize_lot(lot)
        moves = self.env["stock.move.line"].sudo().search(
            [("lot_id", "=", lot.id), ("state", "=", "done")],
            order="date desc, id desc",
            limit=100,
        )
        data["recent_movements"] = [self._serialize_move_line(line) for line in moves]
        return data

    def _serialize_move_line(self, line):
        move = line.move_id
        return {
            "id": str(line.id),
            "move_id": str(move.id),
            "date": line.date.isoformat() if line.date else None,
            "product": {"id": str(line.product_id.id), "name": line.product_id.display_name},
            "lot": (
                {"id": str(line.lot_id.id), "name": line.lot_id.name}
                if line.lot_id
                else None
            ),
            "quantity": line.quantity,
            "uom": line.product_uom_id.name,
            "source_location": line.location_id.complete_name,
            "destination_location": line.location_dest_id.complete_name,
            "source": {
                "model": move.brew_source_model,
                "id": str(move.brew_source_id) if move.brew_source_id else None,
                "reference": move.brew_source_reference or move.reference,
            },
        }

    def movements(self, page, page_size, filters):
        locations = self.env["stock.location"].sudo().search(
            self._location_domain(filters.get("factory_id"), filters.get("location_id"))
        )
        domain = [
            ("company_id", "=", self.company.id),
            ("state", "=", "done"),
            "|",
            ("location_id", "in", locations.ids),
            ("location_dest_id", "in", locations.ids),
        ]
        if filters.get("product_id"):
            domain.append(("product_id", "=", int(filters["product_id"])))
        if filters.get("lot_id"):
            domain.append(("lot_id", "=", int(filters["lot_id"])))
        model = self.env["stock.move.line"].sudo()
        total = model.search_count(domain)
        lines = model.search(
            domain, offset=(page - 1) * page_size, limit=page_size, order="date desc,id desc"
        )
        return [self._serialize_move_line(line) for line in lines], total

    def reservations(self, page, page_size, filters):
        domain = [("company_id", "=", self.company.id)]
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("source_model"):
            domain.append(("source_model", "=", filters["source_model"]))
        if filters.get("source_id"):
            domain.append(("source_id", "=", int(filters["source_id"])))
        model = self.env["brew.inventory.reservation"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [
            {
                "id": str(item.id),
                "product": {
                    "id": str(item.product_id.id),
                    "name": item.product_id.display_name,
                },
                "lot": {"id": str(item.lot_id.id), "name": item.lot_id.name},
                "location": {
                    "id": str(item.location_id.id),
                    "name": item.location_id.complete_name,
                },
                "quantity": item.quantity,
                "uom": item.uom_id.name,
                "source_model": item.source_model,
                "source_id": str(item.source_id),
                "state": item.state,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
            for item in records
        ], total

    def set_lot_frozen(self, lot_id, frozen, reason):
        self._require_manage()
        lot = self._lot(lot_id)
        if frozen:
            lot.action_freeze(reason)
        else:
            lot.action_unfreeze(reason)
        return self._serialize_lot(lot)

    def _plan(self, plan_id):
        plan = self.env["brew.production.plan"].sudo().search(
            [
                ("id", "=", int(plan_id or 0)),
                ("company_id", "=", self.company.id),
                ("factory_id", "in", self._allowed_factory_ids()),
            ],
            limit=1,
        )
        if not plan:
            raise ApiError("RESOURCE_NOT_FOUND", "生产计划不存在。", 404)
        return plan

    def _candidate_stock(self, product, factory, lock=False, excluded_source=None):
        locations = self.env["stock.location"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("usage", "=", "internal"),
                ("brew_factory_id", "=", factory.id),
                ("brew_location_type", "!=", "qc"),
            ]
        )
        if not locations:
            return []
        query = """
            SELECT id
              FROM stock_quant
             WHERE company_id = %s
               AND product_id = %s
               AND location_id = ANY(%s)
               AND quantity > 0
             ORDER BY id
        """
        if lock:
            query += " FOR UPDATE"
        self.env.cr.execute(query, [self.company.id, product.id, locations.ids])
        quant_ids = [row[0] for row in self.env.cr.fetchall()]
        quants = self.env["stock.quant"].sudo().browse(quant_ids)
        if lock:
            quants.invalidate_recordset(["quantity", "reserved_quantity"])
        reservation_totals = self._reservation_totals(
            locations.ids, product.id, excluded_source=excluded_source
        )
        grouped = {}
        today = fields.Date.today()
        for quant in quants:
            lot = quant.lot_id
            if (
                not lot
                or lot.brew_quality_status != "qualified"
                or lot.brew_frozen
                or (lot.brew_expiration_date and lot.brew_expiration_date < today)
            ):
                continue
            key = (quant.location_id.id, lot.id)
            item = grouped.setdefault(
                key,
                {
                    "location": quant.location_id,
                    "lot": lot,
                    "quantity": 0.0,
                    "received_at": quant.in_date,
                },
            )
            item["quantity"] += max(quant.quantity - quant.reserved_quantity, 0)
            if quant.in_date and (
                not item["received_at"] or quant.in_date < item["received_at"]
            ):
                item["received_at"] = quant.in_date
        for key, item in grouped.items():
            item["quantity"] = max(
                item["quantity"]
                - reservation_totals.get((key[0], product.id, key[1]), 0),
                0,
            )
        return sorted(
            (item for item in grouped.values() if item["quantity"] > 0),
            key=lambda item: (
                item["lot"].brew_expiration_date or date.max,
                item["received_at"] or fields.Datetime.now(),
                item["lot"].id,
                item["location"].id,
            ),
        )

    def _plan_materials(self, plan):
        snapshot = json.loads(plan.material_snapshot_json or "{}")
        materials = snapshot.get("materials") if isinstance(snapshot, dict) else None
        if not materials:
            snapshot = plan.expand_materials()
            materials = snapshot.get("materials", [])
        return materials

    def _group_plan_materials(self, materials):
        grouped = {}
        for material in materials:
            product = self._product(material.get("material_id"))
            uom = self.env["uom.uom"].sudo().browse(int(material.get("uom_id") or 0)).exists()
            if not uom or uom.category_id != product.uom_id.category_id:
                raise ApiError("VALIDATION_RELATION", "计划物料单位无效。", 422)
            quantity = uom._compute_quantity(float(material.get("quantity") or 0), product.uom_id, round=False)
            row = grouped.setdefault(product.id, {"material_id": str(product.id), "uom_id": str(product.uom_id.id), "quantity": 0})
            row["quantity"] += quantity
        return list(grouped.values())

    def material_availability(self, materials, factory, excluded_source=None):
        results = []
        for material in self._group_plan_materials(materials):
            product = self._product(material["material_id"])
            required = material["quantity"]
            available = sum(item["quantity"] for item in self._candidate_stock(product, factory, excluded_source=excluded_source))
            results.append({"product": {"id": str(product.id), "name": product.display_name}, "required": required, "available": available, "allocated": min(required, available), "shortage": max(required - available, 0), "uom": product.uom_id.name})
        return results

    def plan_inventory(self, plan_id, reserve=False, expires_at=None):
        plan = self._plan(plan_id)
        if reserve and not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_warehouse")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权为生产计划预占库存。", 403)
        if plan.state in {"done", "cancelled", "terminated"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "当前计划状态不可检查或预占库存。", 422)
        existing = self.env["brew.inventory.reservation"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("source_model", "=", plan._name),
                ("source_id", "=", plan.id),
                ("state", "=", "active"),
            ]
        )
        if reserve:
            existing.action_release()
        results = []
        commands = []
        for material in self._group_plan_materials(self._plan_materials(plan)):
            product = self._product(material.get("material_id"))
            uom = self.env["uom.uom"].sudo().browse(int(material.get("uom_id") or 0)).exists()
            if not uom or uom.category_id != product.uom_id.category_id:
                raise ApiError("VALIDATION_RELATION", "计划物料单位无效。", 422)
            required = uom._compute_quantity(float(material.get("quantity") or 0), product.uom_id, round=False)
            remaining = required
            allocations = []
            for candidate in self._candidate_stock(
                product,
                plan.factory_id,
                lock=reserve,
                excluded_source=(plan._name, plan.id),
            ):
                take = min(candidate["quantity"], remaining)
                if take <= 0:
                    continue
                allocations.append(
                    {
                        "lot_id": str(candidate["lot"].id),
                        "lot_name": candidate["lot"].name,
                        "location_id": str(candidate["location"].id),
                        "location_name": candidate["location"].complete_name,
                        "quantity": take,
                        "uom": product.uom_id.name,
                        "expiration_date": (
                            candidate["lot"].brew_expiration_date.isoformat()
                            if candidate["lot"].brew_expiration_date
                            else None
                        ),
                    }
                )
                if reserve:
                    commands.append(
                        {
                            "company_id": self.company.id,
                            "factory_id": plan.factory_id.id,
                            "product_id": product.id,
                            "lot_id": candidate["lot"].id,
                            "location_id": candidate["location"].id,
                            "quantity": take,
                            "uom_id": product.uom_id.id,
                            "source_model": plan._name,
                            "source_id": plan.id,
                            "expires_at": expires_at,
                        }
                    )
                remaining -= take
                if remaining <= 1e-8:
                    remaining = 0
                    break
            results.append(
                {
                    "product": {"id": str(product.id), "name": product.display_name},
                    "required": required,
                    "allocated": required - remaining,
                    "shortage": max(remaining, 0),
                    "uom": product.uom_id.name,
                    "allocations": allocations,
                }
            )
        shortages = [item for item in results if item["shortage"] > 0]
        if reserve and shortages:
            raise ApiError(
                "INVENTORY_INSUFFICIENT",
                "可用库存不足，未创建任何预占。",
                409,
                {"shortages": shortages},
            )
        reservations = (
            self.env["brew.inventory.reservation"].sudo().create(commands)
            if reserve and commands
            else self.env["brew.inventory.reservation"]
        )
        return {
            "plan_id": str(plan.id),
            "ok": not shortages,
            "reserved": reserve and not shortages,
            "materials": results,
            "reservation_ids": [str(item.id) for item in reservations],
        }

    def release_plan_reservations(self, plan_id):
        plan = self._plan(plan_id)
        records = self.env["brew.inventory.reservation"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("source_model", "=", plan._name),
                ("source_id", "=", plan.id),
                ("state", "=", "active"),
            ]
        )
        count = len(records)
        records.action_release()
        return {"plan_id": str(plan.id), "released_count": count}

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

    def _document(self, model_name, document_id):
        document = self.env[model_name].sudo().search(
            [
                ("id", "=", int(document_id or 0)),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not document:
            raise ApiError("RESOURCE_NOT_FOUND", "库存单据不存在。", 404)
        factory = document.factory_id
        if factory and factory.id not in self._allowed_factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "库存单据不在当前账号数据范围内。", 403)
        return document

    def _line_values(self, payload):
        product = self._product(payload.get("product_id"))
        lot = self._lot(payload.get("lot_id"))
        uom = self.env["uom.uom"].sudo().browse(int(payload.get("uom_id") or 0)).exists()
        if lot.product_id != product:
            raise ApiError("VALIDATION_RELATION", "批次与产品不一致。", 422)
        if not uom or uom.category_id != product.uom_id.category_id:
            raise ApiError("VALIDATION_RELATION", "计量单位与产品不一致。", 422)
        quantity = float(payload.get("quantity") or 0)
        if quantity <= 0:
            raise ApiError("VALIDATION_RANGE", "数量必须大于零。", 422)
        return {
            "product_id": product.id,
            "lot_id": lot.id,
            "quantity": quantity,
            "uom_id": uom.id,
        }

    def _serialize_transfer(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "source_location": {
                "id": str(record.source_location_id.id),
                "name": record.source_location_id.complete_name,
            },
            "destination_location": {
                "id": str(record.destination_location_id.id),
                "name": record.destination_location_id.complete_name,
            },
            "reason": record.reason,
            "picking_id": str(record.picking_id.id) if record.picking_id else None,
            "lines": [
                {
                    "id": str(line.id),
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name},
                    "quantity": line.quantity,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def transfers(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._allowed_factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.inventory.transfer"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_transfer(record) for record in records], total

    def create_transfer(self, payload):
        self._require_manage()
        factory = self._factory(payload.get("factory_id"))
        source = self._location(payload.get("source_location_id"))
        destination = self._location(payload.get("destination_location_id"))
        if source.brew_factory_id != factory or destination.brew_factory_id != factory:
            raise ApiError("VALIDATION_RELATION", "调拨库位必须属于所选厂区。", 422)
        reason = str(payload.get("reason") or "").strip()
        lines = payload.get("lines") or []
        if not reason or not lines:
            raise ApiError("VALIDATION_REQUIRED", "调拨原因和明细不能为空。", 422)
        record = self.env["brew.inventory.transfer"].sudo().create(
            {
                "company_id": self.company.id,
                "factory_id": factory.id,
                "source_location_id": source.id,
                "destination_location_id": destination.id,
                "reason": reason,
                "line_ids": [Command.create(self._line_values(line)) for line in lines],
            }
        )
        return record, self._serialize_transfer(record)

    def confirm_transfer(self, document_id, version):
        self._require_manage()
        record = self._document("brew.inventory.transfer", document_id)
        self._check_version(record, version)
        record.action_confirm()
        return record, self._serialize_transfer(record)

    def _serialize_count(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": (
                {"id": str(record.factory_id.id), "name": record.factory_id.name}
                if record.factory_id
                else None
            ),
            "location": {
                "id": str(record.location_id.id),
                "name": record.location_id.complete_name,
            },
            "approval_instance_id": (
                str(record.approval_instance_id.id)
                if record.approval_instance_id
                else None
            ),
            "lines": [
                {
                    "id": str(line.id),
                    "location": {
                        "id": str(line.location_id.id),
                        "name": line.location_id.complete_name,
                    },
                    "product": {
                        "id": str(line.product_id.id),
                        "name": line.product_id.display_name,
                    },
                    "lot": (
                        {"id": str(line.lot_id.id), "name": line.lot_id.name}
                        if line.lot_id
                        else None
                    ),
                    "theoretical_qty": line.theoretical_qty,
                    "counted_qty": line.counted_qty,
                    "difference_qty": line.difference_qty,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def counts(self, page, page_size, state=None):
        location_ids = self.env["stock.location"].sudo().search(
            self._location_domain()
        ).ids
        domain = [
            ("company_id", "=", self.company.id),
            ("location_id", "in", location_ids),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.inventory.count"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_count(record) for record in records], total

    def create_count(self, payload):
        self._require_manage()
        location = self._location(payload.get("location_id"))
        if not location.brew_factory_id:
            raise ApiError("VALIDATION_RELATION", "盘点库位必须归属厂区。", 422)
        record = self.env["brew.inventory.count"].sudo().create(
            {"company_id": self.company.id, "location_id": location.id}
        )
        return record, self._serialize_count(record)

    def count_action(self, document_id, action, payload):
        self._require_manage()
        record = self._document("brew.inventory.count", document_id)
        self._check_version(record, payload.get("version"))
        if action == "start":
            record.action_start()
        elif action == "submit":
            record.action_submit(self.user)
        elif action == "confirm":
            record.action_confirm()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "盘点动作不存在。", 404)
        return record, self._serialize_count(record)

    def update_count_lines(self, document_id, payload):
        self._require_manage()
        record = self._document("brew.inventory.count", document_id)
        self._check_version(record, payload.get("version"))
        if record.state != "counting":
            raise ApiError("STATE_PRECONDITION_FAILED", "只有盘点中单据可录入实数。", 422)
        incoming = payload.get("lines")
        if not isinstance(incoming, list) or not incoming:
            raise ApiError("VALIDATION_REQUIRED", "盘点实数不能为空。", 422)
        by_id = {line.id: line for line in record.line_ids}
        seen = set()
        for values in incoming:
            line_id = int(values.get("id") or 0)
            line = by_id.get(line_id)
            if not line or line_id in seen:
                raise ApiError("VALIDATION_RELATION", "盘点明细不存在或重复。", 422)
            counted = float(values.get("counted_qty"))
            if counted < 0:
                raise ApiError("VALIDATION_RANGE", "盘点实数不能小于零。", 422)
            line.write({"counted_qty": counted})
            seen.add(line_id)
        if seen != set(by_id):
            raise ApiError("VALIDATION_REQUIRED", "必须提交全部盘点明细。", 422)
        return record, self._serialize_count(record)

    def _serialize_scrap(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "location": {
                "id": str(record.location_id.id),
                "name": record.location_id.complete_name,
            },
            "reason": record.reason,
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
                    "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name},
                    "quantity": line.quantity,
                    "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name},
                }
                for line in record.line_ids
            ],
            "version": self._version(record),
        }

    def scraps(self, page, page_size, state=None):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self._allowed_factory_ids()),
        ]
        if state:
            domain.append(("state", "=", state))
        model = self.env["brew.inventory.scrap"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="create_date desc,id desc",
        )
        return [self._serialize_scrap(record) for record in records], total

    def create_scrap(self, payload):
        self._require_manage()
        factory = self._factory(payload.get("factory_id"))
        location = self._location(payload.get("location_id"))
        reason = str(payload.get("reason") or "").strip()
        lines = payload.get("lines") or []
        if location.brew_factory_id != factory:
            raise ApiError("VALIDATION_RELATION", "报损库位必须属于所选厂区。", 422)
        if not reason or not lines:
            raise ApiError("VALIDATION_REQUIRED", "报损原因和明细不能为空。", 422)
        record = self.env["brew.inventory.scrap"].sudo().create(
            {
                "company_id": self.company.id,
                "factory_id": factory.id,
                "location_id": location.id,
                "reason": reason,
                "line_ids": [Command.create(self._line_values(line)) for line in lines],
            }
        )
        record.action_submit(self.user)
        return record, self._serialize_scrap(record)

    def confirm_scrap(self, document_id, version):
        self._require_manage()
        record = self._document("brew.inventory.scrap", document_id)
        self._check_version(record, version)
        record.action_confirm()
        return record, self._serialize_scrap(record)
