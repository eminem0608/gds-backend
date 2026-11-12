import json
import math

from odoo import fields
from odoo.tools.float_utils import float_is_zero

from ..controllers.base import ApiError


class InventoryCountServiceMixin:
    def _count_for_update(self, document_id, payload):
        self._require_manage()
        self.env["brew.inventory.count"]._lock_stock([self.company.id])
        record = self._document("brew.inventory.count", document_id)._locked()
        self._check_version(record, payload.get("version"))
        return record

    def _serialize_count(self, record, compact=False):
        differences = record.line_ids.filtered(lambda line: line.is_counted and not float_is_zero(line.difference_qty, precision_rounding=line.uom_id.rounding))
        result = {
            "id": str(record.id), "name": record.name, "state": record.state,
            "approval_state": record.approval_instance_id.state or None,
            "approval_instance_id": str(record.approval_instance_id.id) if record.approval_instance_id else None,
            "created_at": record.create_date.isoformat() if record.create_date else None,
            "updated_at": record.write_date.isoformat() if record.write_date else None,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "owner": {"id": str(record.owner_id.id), "name": record.owner_id.name},
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "location": {"id": str(record.location_id.id), "name": record.location_id.complete_name},
            "scope_locked": record.scope_locked, "cancel_reason": record.cancel_reason,
            "can_edit": record._editable_count() and record.scope_locked,
            "line_count": len(record.line_ids), "counted_count": len(record.line_ids.filtered("is_counted")),
            "difference_count": len(differences), "version": self._version(record),
        }
        if compact:
            return result
        result["lines"] = [{
            "id": str(line.id),
            "location": {"id": str(line.location_id.id), "name": line.location_id.complete_name},
            "product": {"id": str(line.product_id.id), "name": line.product_id.display_name, "code": line.product_id.default_code or ""},
            "lot": {"id": str(line.lot_id.id), "name": line.lot_id.name} if line.lot_id else None,
            "theoretical_qty": line.theoretical_qty, "counted_qty": line.counted_qty if line.is_counted else None,
            "is_counted": line.is_counted, "difference_qty": line.difference_qty if line.is_counted else None,
            "difference_reason": line.difference_reason or "", "difference_note": line.difference_note or "",
            "recounts": json.loads(line.recounts_json or "[]"),
            "counted_at": line.counted_at.isoformat() if line.counted_at else None,
            "counted_by": line.counted_by_id.name or None,
            "previous_count": {"name": line.previous_count_line_id.count_id.name, "counted_qty": line.previous_count_line_id.counted_qty} if line.previous_count_line_id else None,
            "uom": {"id": str(line.uom_id.id), "name": line.uom_id.name, "rounding": line.uom_id.rounding},
        } for line in record.line_ids]
        result["history"] = json.loads(record.history_json or "[]")
        approvals = self.env["brew.approval.instance"].sudo().search([
            ("company_id", "=", self.company.id), ("target_model", "=", record._name), ("target_id", "=", record.id),
        ], order="id desc")
        roles = set(self.user.brew_role_codes())
        result["approvals"] = [{
            "id": str(instance.id), "state": instance.state,
            "steps": [{"id": str(step.id), "role": step.role_code, "state": step.state,
                       "by": step.acted_by_id.name or None, "comment": step.comment or "",
                       "at": step.acted_at.isoformat() if step.acted_at else None,
                       "version": step.write_date.isoformat() if step.write_date else None,
                       "can_act": instance == record.approval_instance_id and instance.state == "pending" and step.state == "pending" and step.sequence == instance.current_sequence and step.role_code in roles and (not step.assignee_id or step.assignee_id == self.user)}
                      for step in instance.step_ids.sorted("sequence")],
        } for instance in approvals]
        moves = self.env["stock.move"].sudo().search([
            ("company_id", "=", self.company.id), ("brew_source_model", "=", record._name),
            ("brew_source_id", "=", record.id), ("state", "=", "done"),
        ], order="id")
        result["movements"] = [{"id": str(move.id), "name": move.reference or move.name,
                                "product": move.product_id.display_name, "quantity": move.quantity,
                                "uom": move.product_uom.name, "direction": "increase" if move.location_id.usage == "inventory" else "decrease"}
                               for move in moves]
        return result

    def counts(self, page, page_size, state=None, filters=None):
        filters = filters or {}
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self._allowed_factory_ids())]
        if filters.get("factory_id"):
            domain.append(("factory_id", "=", self._factory(filters["factory_id"]).id))
        if filters.get("keyword"):
            term = filters["keyword"].strip()
            domain += ["|", ("name", "ilike", term), ("location_id.complete_name", "ilike", term)]
        if state == "returned":
            domain += [("state", "=", "pending_approval"), ("approval_instance_id.state", "=", "returned")]
        elif state == "approved":
            domain += [("state", "=", "pending_approval"), ("approval_instance_id.state", "=", "approved")]
        elif state == "pending_approval":
            domain += [("state", "=", "pending_approval"), ("approval_instance_id.state", "=", "pending")]
        elif state:
            domain.append(("state", "=", state))
        model = self.env["brew.inventory.count"].sudo()
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        compact = str(filters.get("compact", "")).lower() in {"true", "1"}
        return [self._serialize_count(record, compact=compact) for record in records], model.search_count(domain)

    def count_options(self, payload):
        factories = self.env["brew.factory"].sudo().browse(self._allowed_factory_ids())
        if payload.get("factory_id"):
            factories = self._factory(payload["factory_id"])
        locations = self.env["stock.location"].sudo().search([
            ("company_id", "=", self.company.id), ("usage", "=", "internal"), ("brew_factory_id", "in", factories.ids),
        ], order="complete_name")
        domain = [("detailed_type", "=", "product"), "|", ("company_id", "=", False), ("company_id", "=", self.company.id),
                  "|", ("brew_item_type", "!=", "packaging_material"), ("brew_packaging_catalog_owner_id", "=", False)]
        if payload.get("keyword"):
            domain += ["|", ("name", "ilike", payload["keyword"].strip()), ("default_code", "ilike", payload["keyword"].strip())]
        products = self.env["product.product"].sudo().search(domain, limit=80, order="name,id")
        lots = self.env["stock.lot"]
        if payload.get("product_id"):
            product = self._product(payload["product_id"])
            lots = self.env["stock.lot"].sudo().search([
                ("company_id", "=", self.company.id), ("product_id", "=", product.id),
            ], order="name", limit=200)
        return {"factories": [{"id": str(f.id), "name": f.name} for f in factories],
                "locations": [{"id": str(loc.id), "name": loc.complete_name, "factory_id": str(loc.brew_factory_id.id), "usage": loc.usage} for loc in locations],
                "products": [{"id": str(p.id), "name": p.display_name, "tracking": p.tracking, "uom": p.uom_id.name} for p in products],
                "lots": [{"id": str(lot.id), "name": lot.name} for lot in lots]}

    def create_count(self, payload):
        self._require_manage()
        location = self._location(payload.get("location_id"))
        if not location.brew_factory_id:
            raise ApiError("VALIDATION_RELATION", "盘点库位必须归属厂区。", 422)
        self._factory(location.brew_factory_id.id)
        record = self.env["brew.inventory.count"].sudo().create({
            "company_id": self.company.id, "location_id": location.id, "owner_id": self.user.id,
        })
        record._log_count(self.user, "新建盘点", location.complete_name)
        return record, self._serialize_count(record)

    def count_action(self, document_id, action, payload):
        with self.env.cr.savepoint():
            record = self._count_for_update(document_id, payload)
            acting = record.with_user(self.user).with_company(self.company).sudo()
            if action == "start":
                acting.action_start()
            elif action == "submit":
                acting.action_submit(self.user)
            elif action == "confirm":
                acting.action_confirm()
            elif action == "cancel":
                acting.action_cancel(self.user, payload.get("reason"))
            else:
                raise ApiError("RESOURCE_NOT_FOUND", "盘点动作不存在。", 404)
            return record, self._serialize_count(record)

    @staticmethod
    def _count_number(value):
        try:
            if isinstance(value, bool) or value is None or value == "":
                raise ValueError()
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ApiError("VALIDATION_RANGE", "实盘数量必须是大于等于零的有效数字。", 422)
        if not math.isfinite(number) or number < 0:
            raise ApiError("VALIDATION_RANGE", "实盘数量必须是大于等于零的有效数字。", 422)
        return number

    def update_count_lines(self, document_id, payload):
        with self.env.cr.savepoint():
            record = self._count_for_update(document_id, payload)
            if not record._editable_count() or not record.scope_locked:
                raise ApiError("STATE_PRECONDITION_FAILED", "仅有有效快照的盘点中或审批退回单可修改。", 422)
            incoming = payload.get("lines")
            if not isinstance(incoming, list):
                raise ApiError("VALIDATION_REQUIRED", "请提交盘点明细。", 422)
            by_id = {str(line.id): line for line in record.line_ids}
            seen = set()
            allowed_reasons = dict(self.env["brew.inventory.count.line"]._fields["difference_reason"].selection)
            for values in incoming:
                if not isinstance(values, dict):
                    raise ApiError("VALIDATION_FORMAT", "盘点明细格式不正确。", 422)
                line_id = str(values.get("id") or "")
                line = by_id.get(line_id)
                if not line or line_id in seen:
                    raise ApiError("VALIDATION_RELATION", "盘点明细不存在或重复。", 422)
                seen.add(line_id)
                raw = values.get("counted_qty")
                counted = raw is not None and raw != ""
                number = self._count_number(raw) if counted else 0
                reason = values.get("difference_reason") or False
                note = str(values.get("difference_note") or "").strip()
                if reason and reason not in allowed_reasons:
                    raise ApiError("VALIDATION_RANGE", "差异原因不存在。", 422)
                changed = counted != line.is_counted or number != line.counted_qty or reason != line.difference_reason or note != (line.difference_note or "")
                recount_note = str(values.get("recount_note") or "").strip()
                changes = {"counted_qty": number, "is_counted": counted,
                           "difference_reason": reason, "difference_note": note}
                if changed:
                    changes.update(counted_at=fields.Datetime.now() if counted else False, counted_by_id=self.user.id if counted else False)
                if recount_note:
                    if not counted or not line.is_counted:
                        raise ApiError("STATE_PRECONDITION_FAILED", "请先录入首次实盘，再记录复盘。", 422)
                    history = json.loads(line.recounts_json or "[]")
                    history.append({"before": line.counted_qty, "after": number, "by": self.user.name,
                                    "at": fields.Datetime.now().isoformat(), "note": recount_note})
                    changes["recounts_json"] = json.dumps(history, ensure_ascii=False)
                line.write(changes)
            record._log_count(self.user, "暂存实盘", f"更新 {len(seen)} 项")
            if payload.get("submit_after_save"):
                record.action_submit(self.user)
            return record, self._serialize_count(record)

    def add_count_line(self, document_id, payload):
        with self.env.cr.savepoint():
            record = self._count_for_update(document_id, payload)
            if not record._editable_count() or not record.scope_locked:
                raise ApiError("STATE_PRECONDITION_FAILED", "当前盘点单不能补录。", 422)
            location = self._location(payload.get("location_id"))
            scope = self.env["stock.location"].sudo().search([("id", "child_of", record.location_id.id)])
            if location not in scope or location.brew_factory_id != record.factory_id:
                raise ApiError("DATA_SCOPE_DENIED", "只能补录本单盘点范围内的实物。", 403)
            product = self._product(payload.get("product_id"))
            if product.detailed_type != "product":
                raise ApiError("VALIDATION_RELATION", "只能盘点管理实物库存的商品。", 422)
            lot = self._lot(payload["lot_id"]) if payload.get("lot_id") else self.env["stock.lot"]
            if (lot and lot.product_id != product) or (product.tracking != "none" and not lot):
                raise ApiError("VALIDATION_RELATION", "请选择与商品匹配的已有批次。", 422)
            if record.line_ids.filtered(lambda line: line.location_id == location and line.product_id == product and line.lot_id == lot):
                raise ApiError("VALIDATION_RELATION", "该商品批次已在本库位明细中，请修改原明细。", 422)
            quantity = self._count_number(payload.get("counted_qty"))
            note = str(payload.get("difference_note") or "").strip()
            if quantity <= 0 or not note:
                raise ApiError("VALIDATION_REQUIRED", "补录数量必须大于零，并填写发现说明。", 422)
            record._validate_snapshot()
            self.env["brew.inventory.count.line"].sudo().create({
                "count_id": record.id, "location_id": location.id, "product_id": product.id,
                "lot_id": lot.id or False, "uom_id": product.uom_id.id, "theoretical_qty": 0,
                "counted_qty": quantity, "is_counted": True, "difference_reason": "other",
                "difference_note": note, "counted_at": fields.Datetime.now(), "counted_by_id": self.user.id,
            })
            record._log_count(self.user, "补录发现库存", product.display_name)
            return record, self._serialize_count(record)
