import json
import math
import logging
from types import SimpleNamespace
from datetime import datetime, timezone

from odoo import fields
from odoo.exceptions import ValidationError
from psycopg2.extensions import TransactionRollbackError
from .inventory import InventoryService
from .production_lifecycle import can_close, require_close, close_plan
from .packaging_specifications import ACTIVE_TYPES

from ..controllers.base import ApiError
from .recipe_design import design_values, mash_water_requirements


_logger = logging.getLogger(__name__)


class PlanningService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not any(
            user.has_group(group)
            for group in {
                "craft_brew_core.group_brew_general_manager",
                "craft_brew_core.group_brew_brewery_manager",
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
        if plan.factory_id not in self.user.brew_allowed_factories(self.company):
            raise ApiError("DATA_SCOPE_DENIED", "计划不在当前账号厂区范围内。", 403)
        if self.user.has_group("craft_brew_core.group_brew_operator") and not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director"}):
            if self.user not in (plan.mash_owner_id | plan.fermentation_owner_id | plan.packaging_owner_id):
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
            "packaging_owner": {"id": str(plan.packaging_owner_id.id), "name": plan.packaging_owner_id.name} if plan.packaging_owner_id else None,
            "packaging_plan": json.loads(plan.packaging_plan_json or "{}"),
            "packaging_requirement": {"packaging_method": plan.demand_id.packaging_method, "package_container_type": plan.demand_id.package_container_type, "package_unit_volume_l": plan.demand_id.package_unit_volume_l, "package_units_per_box": plan.demand_id.package_units_per_box, "package_qty": plan.demand_id.package_qty} if plan.demand_id else None,
            "recipe_version_type": "production" if plan.production_design_json and plan.production_design_json != "{}" else "base",
            "approval_instance_id": str(plan.approval_instance_id.id) if plan.approval_instance_id else None,
            "dispatched_at": plan.dispatched_at.isoformat() if plan.dispatched_at else None,
            "cancel_reason": plan.cancel_reason,
            "can_close": can_close(self.user) and not bool(plan.deleted_at),
            "deleted_at": plan.deleted_at.isoformat() if plan.deleted_at else None,
            "closed_at": plan.closed_at.isoformat() if plan.closed_at else None,
            "closed_by": plan.closed_by_id.name or None,
            "brewing_completion": json.loads(plan.brewing_completion_json or "{}"),
            "version": plan.write_date.isoformat() if plan.write_date else None,
        }
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
        data["batch"] = {"id": str(batch.id), "name": batch.name, "state": batch.state,
                         "finished_product_code": batch.finished_product_code, "product_code": batch.product_code_snapshot} if batch else None
        if detail:
            data["stocking_request"] = json.loads(plan.stocking_request_json or "{}")
            data.update({"resource_check": json.loads(plan.resource_check_json or "{}"), "recipe_snapshot": json.loads(plan.recipe_snapshot_json or "{}"), "material_snapshot": json.loads(plan.material_snapshot_json or "[]"), "production_design": json.loads(plan.production_design_json or "{}")})
        return data

    def list(self, page, page_size, state=None):
        model = self.env["brew.production.plan"].sudo()
        domain = [("company_id", "=", self.company.id), ("deleted_at", "=", False)]
        domain.append(("factory_id", "in", self.user.brew_allowed_factories(self.company).ids))
        if not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director"}):
            domain += ["|", "|", ("mash_owner_id", "=", self.user.id), ("fermentation_owner_id", "=", self.user.id), ("packaging_owner_id", "=", self.user.id)]
        if state:
            domain += [("state", "=", state)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self.serialize(item) for item in records], total

    def assignees(self, factory_id):
        self._require_manage()
        factory = self.env["brew.factory"].sudo().search(
            [("id", "=", int(factory_id or 0)), ("company_id", "=", self.company.id), ("active", "=", True)], limit=1
        )
        if not factory or factory not in self.user.brew_allowed_factories(self.company):
            raise ApiError("DATA_SCOPE_DENIED", "厂区无效，无法加载生产负责人。", 403)
        eligible_group = "craft_brew_core.group_brew_operator"
        users = self.env["res.users"].sudo().search(
            [("company_ids", "in", [self.company.id]), ("active", "=", True), ("share", "=", False)],
            order="name,id",
        )
        return [
            {"id": str(user.id), "name": user.name}
            for user in users
            if user.has_group(eligible_group) and factory in user.brew_allowed_factories(self.company)
        ]

    def _assignee(self, user_id, factory):
        if not user_id:
            return self.env["res.users"]
        user = self.env["res.users"].sudo().search(
            [("id", "=", int(user_id)), ("company_ids", "in", [self.company.id]), ("active", "=", True), ("share", "=", False)],
            limit=1,
        )
        if not user or factory not in user.brew_allowed_factories(self.company) or not user.has_group("craft_brew_core.group_brew_operator"):
            raise ApiError("DATA_SCOPE_DENIED", "负责人必须是该厂区有权限的操作人员。", 403)
        return user

    def packaging_options(self, product_tmpl_id, factory_id=None):
        self._require_manage()
        product = self.env["product.template"].sudo().search([("id", "=", int(product_tmpl_id or 0)), ("company_id", "in", [False, self.company.id]), ("brew_item_type", "=", "finished_product")], limit=1)
        if not product:
            raise ApiError("DATA_SCOPE_DENIED", "请选择当前企业的成品产品。", 403)
        inventory = InventoryService(self.env, self.user, self.company)
        factory = inventory._factory(factory_id) if factory_id else None
        materials = self.env["product.template"].sudo().search([("brew_item_type", "=", "packaging_material"), ("brew_packaging_type", "in", list(ACTIVE_TYPES)), ("brew_packaging_catalog_owner_id", "=", False), ("company_id", "=", self.company.id), ("brew_packaging_state", "=", "confirmed"), ("active", "=", True)])
        result = []
        for material in materials:
            details = material.brew_packaging_details or {}
            capacity = float(details.get("capacity") or 0)
            volume = capacity / 1000 if details.get("capacity_unit") == "mL" else capacity if details.get("capacity_unit") == "L" else 0
            result.append({"id": str(material.id), "product_id": str(material.product_variant_id.id), "name": material.name, "type": material.brew_packaging_type, "volume_l": volume, "pack_count": details.get("pack_count"), "unit": details.get("unit") or material.uom_id.name, "specification": material.brew_specification, "supplier": material.brew_supplier_id.name or "酒厂材料"})
            result[-1]["available_qty"] = sum(item["quantity"] for item in inventory._candidate_stock(material.product_variant_id, factory)) if factory else None
            result[-1].update(fit_container_type=details.get("fit_container_type"), fit_capacity_ml=details.get("fit_capacity_ml"))
            result[-1]["unit_price"] = material.brew_supplier_price if details.get("quote_price") not in [None, ""] else None
        return result

    def _packaging_material(self, material_id, product, check_product=True):
        material = self.env["product.template"].sudo().search([("id", "=", int(material_id or 0)), ("company_id", "=", self.company.id), ("brew_item_type", "=", "packaging_material"), ("brew_packaging_type", "in", list(ACTIVE_TYPES)), ("brew_packaging_catalog_owner_id", "=", False), ("brew_packaging_state", "=", "confirmed"), ("active", "=", True)], limit=1)
        details = material.brew_packaging_details or {}
        if not material or check_product and details.get("custom") and str(details.get("beer_product_id")) != str(product.id):
            raise ApiError("VALIDATION_RELATION", "请选择已确认且适用于本产品的包装材料。", 422)
        return material

    def _packaging_values(self, payload, factory, product, volume, brewing_end, demand=None):
        data = payload.get("packaging_plan") or {}
        if not data:
            return {"packaging_plan_json": "{}", "packaging_owner_id": False}
        if not isinstance(data, dict):
            raise ApiError("VALIDATION_FORMAT", "包装计划格式无效。", 422)
        owner = self._assignee(payload.get("packaging_owner_id"), factory)
        if not owner:
            raise ApiError("VALIDATION_REQUIRED", "请指定包装执行人。", 422)
        if data.get("materials_only"):
            lines = []
            seen = set()
            for line in data.get("materials") or []:
                material = self._packaging_material(line.get("material_id"), product, check_product=False)
                if material.id in seen:
                    raise ApiError("VALIDATION_DUPLICATE", "同一种包装材料请合并填写数量。", 422)
                seen.add(material.id)
                try:
                    quantity = float(line.get("quantity"))
                    if isinstance(line.get("quantity"), bool) or not math.isfinite(quantity) or quantity <= 0 or not quantity.is_integer():
                        raise ValueError()
                except (TypeError, ValueError):
                    raise ApiError("VALIDATION_RANGE", "包装材料数量须为正整数。", 422)
                lines.append({"material_id": str(material.id), "product_id": str(material.product_variant_id.id), "name": material.name, "quantity": int(quantity), "unit": "个"})
            if not lines:
                raise ApiError("VALIDATION_REQUIRED", "请至少添加一种包装材料。", 422)
            start = self._plan_datetime(data.get("planned_start"), "包装开始时间")
            end = self._plan_datetime(data.get("planned_end"), "包装结束时间")
            if end <= start or start < brewing_end:
                raise ApiError("VALIDATION_RANGE", "包装须安排在预计酿酒结束后，且结束时间晚于开始时间。", 422)
            values = {"materials_only": True, "materials": lines, "packaging_name": "、".join(line["name"] for line in lines), "planned_start": start.isoformat(), "planned_end": end.isoformat(), "notes": str(data.get("notes") or "").strip()}
            if data.get("finished_packaging_id"):
                package = self.env["product.packaging"].sudo().search([
                    ("id", "=", int(data["finished_packaging_id"])),
                    ("product_id.product_tmpl_id", "=", product.id),
                    ("product_id.product_tmpl_id.company_id", "=", self.company.id),
                    ("active", "=", True)], limit=1)
                if not package:
                    raise ApiError("VALIDATION_RELATION", "请选择本酒款已启用的包装规格。", 422)
                values.update(finished_packaging_id=str(package.id), packaging_barcode=package.barcode,
                              finished_product_name=package.name)
            return {"packaging_plan_json": json.dumps(values, ensure_ascii=False), "packaging_owner_id": owner.id}
        material = self._packaging_material(data["material_id"], product) if data.get("material_id") else None
        if material:
            details = material.brew_packaging_details or {}
            capacity = float(details.get("capacity") or 0)
            unit_volume = capacity / 1000 if details.get("capacity_unit") == "mL" else capacity if details.get("capacity_unit") == "L" else 0
            if material.brew_packaging_type not in {"keg", "bottle", "can"} or unit_volume <= 0:
                raise ApiError("VALIDATION_RELATION", "主包装材料须选择有明确容量的桶、瓶或罐。", 422)
            method = data.get("packaging_method") or (demand.packaging_method if demand else None) or ("keg" if material.brew_packaging_type == "keg" else "other")
            if method not in {"keg", "box", "other"} or method == "keg" and material.brew_packaging_type != "keg" or method == "box" and material.brew_packaging_type not in {"bottle", "can"}:
                raise ApiError("VALIDATION_RELATION", "主包装材料类型须与包装方式一致。", 422)
            try:
                units_raw = float(data.get("units_per_box") or (demand.package_units_per_box if demand else 1)) if method == "box" else 1.0
                if not math.isfinite(units_raw) or not units_raw.is_integer():
                    raise ValueError()
                units_per_box = int(units_raw)
            except (TypeError, ValueError):
                raise ApiError("VALIDATION_RANGE", "每箱包装数量须为正整数。", 422)
            if units_per_box < 1:
                raise ApiError("VALIDATION_RANGE", "请填写每箱包装数量。", 422)
            if demand and demand.packaging_method in {"keg", "box"}:
                container = "keg" if demand.packaging_method == "keg" else demand.package_container_type
                mismatch = (container and material.brew_packaging_type != container) or (demand.package_unit_volume_l and abs(unit_volume - demand.package_unit_volume_l) > 1e-8) or method != demand.packaging_method or (method == "box" and demand.package_units_per_box and units_per_box != demand.package_units_per_box)
                if mismatch and data.get("confirm_packaging_change") is not True:
                    raise ApiError("VALIDATION_RELATION", "包装材料的类型和容量必须与产品需求一致。", 422)
            package_volume = unit_volume * units_per_box
            package_name = material.name
        else:
            package = self.env["product.packaging"].sudo().search([("id", "=", int(data.get("packaging_id") or 0)), ("product_id.product_tmpl_id", "=", product.id), ("company_id", "in", [False, self.company.id]), ("active", "=", True), ("brew_volume_l", ">", 0)], limit=1)
            if not package:
                raise ApiError("VALIDATION_RELATION", "请选择本产品对应的包装材料。", 422)
            package_volume, package_name = package.brew_volume_l, package.name
        try:
            quantity = float(data.get("planned_qty"))
            if isinstance(data.get("planned_qty"), bool) or not math.isfinite(quantity) or quantity <= 0 or not quantity.is_integer():
                raise ValueError()
        except (ValueError, TypeError):
            raise ApiError("VALIDATION_RANGE", "计划包装数量必须是正整数。", 422)
        start = self._plan_datetime(data.get("planned_start"), "包装开始时间")
        end = self._plan_datetime(data.get("planned_end"), "包装结束时间")
        if end <= start or start < brewing_end:
            raise ApiError("VALIDATION_RANGE", "包装须安排在预计酿酒结束后，且结束时间晚于开始时间。", 422)
        planned_volume = quantity * package_volume
        if planned_volume > float(volume) + 1e-8:
            raise ApiError("VALIDATION_RANGE", "计划包装酒液量不能超过计划产量。", 422)
        values = {"packaging_name": package_name, "unit_volume_l": package_volume, "planned_qty": int(quantity), "planned_volume_l": planned_volume, "planned_start": start.isoformat(), "planned_end": end.isoformat(), "notes": str(data.get("notes") or "").strip()}
        if material:
            values.update(material_id=str(material.id), material_product_id=str(material.product_variant_id.id), packaging_method=method, units_per_box=units_per_box, container_volume_l=unit_volume)
            values["confirm_packaging_change"] = data.get("confirm_packaging_change") is True
            lines = [{"material_id": str(material.id), "product_id": str(material.product_variant_id.id), "name": material.name, "quantity": int(quantity) * units_per_box, "unit": material.uom_id.name}]
            seen = {material.id}
            for line in data.get("materials") or []:
                if line.get("primary") and str(line.get("material_id")) == str(material.id):
                    continue
                accessory = self._packaging_material(line.get("material_id"), product)
                if accessory.id in seen or accessory.brew_packaging_type in {"bottle", "can", "keg"}:
                    raise ApiError("VALIDATION_RELATION", "辅材不得重复或选择主容器材料。", 422)
                seen.add(accessory.id)
                allowed_types = {"carton"}
                if accessory.brew_packaging_type not in allowed_types:
                    raise ApiError("VALIDATION_RELATION", "包装辅材类型与主容器不匹配。", 422)
                try:
                    needed = float(line.get("quantity"))
                    if not math.isfinite(needed) or needed <= 0:
                        raise ValueError()
                    if accessory.uom_id == self.env.ref("uom.product_uom_unit") and not needed.is_integer():
                        raise ValueError()
                except (TypeError, ValueError):
                    raise ApiError("VALIDATION_RANGE", "包装辅材用量必须大于零。", 422)
                if accessory.brew_packaging_type == "carton" and method == "box" and int((accessory.brew_packaging_details or {}).get("pack_count") or 0) != units_per_box:
                    raise ApiError("VALIDATION_RELATION", "纸箱装载数量须与产品需求的每箱数量一致。", 422)
                if accessory.brew_packaging_type == "carton":
                    carton = accessory.brew_packaging_details or {}
                    container_name = {"bottle": "酒瓶", "can": "易拉罐", "keg": "酒桶"}[material.brew_packaging_type]
                    if carton.get("fit_container_type") and carton["fit_container_type"] != container_name or carton.get("fit_capacity_ml") and abs(float(carton["fit_capacity_ml"]) / 1000 - unit_volume) > 1e-8:
                        raise ApiError("VALIDATION_RELATION", "纸箱的适用容器和容量须与主包装材料一致。", 422)
                lines.append({"material_id": str(accessory.id), "product_id": str(accessory.product_variant_id.id), "name": accessory.name, "quantity": needed, "unit": accessory.uom_id.name})
            lines[0]["primary"] = True
            values["materials"] = lines
        else:
            values["packaging_id"] = str(package.id)
        return {"packaging_plan_json": json.dumps(values, ensure_ascii=False), "packaging_owner_id": owner.id}

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
        normalized = design_values(self.env, self.company, design, plan.planned_volume_l, self.user, recipe.product_tmpl_id.id)
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
        from odoo.addons.craft_brew_workflow.models.water_chemistry import water_volumes, scale_additions
        old_water_volumes = water_volumes(result)
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
        _pre_volume, sparge = mash_water_requirements(mash, volume, grain)
        count = max(1, int(float(mash.get("sparge_count") or 1)))
        each = math.floor(sparge * 100 / count) / 100
        mash["sparge_water_liters"] = [round(sparge - each * (count - 1), 2) if index == count - 1 else each for index in range(count)]
        scale_additions(result,old_water_volumes)
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
            if source_batch.factory_id not in self.user.brew_allowed_factories(self.company):
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
        if tank.current_volume_l > 0 or tank.current_production_batch_id or tank.status != 'empty':
            raise ApiError('TANK_NOT_EMPTY', f'酒罐 {tank.name} 当前不是空罐（在罐 {tank.current_volume_l:g} L），请先完成出酒或选择其他空罐。', 422)
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
        if payload.get("product_tmpl_id") and str(payload["product_tmpl_id"]) != str(version.recipe_id.product_tmpl_id.id):
            raise ApiError("VALIDATION_RELATION", "所选酒品与配方对应的产品不一致。", 422)
        owner = self._assignee(payload.get("owner_id"), factory)
        mash_owner = self._assignee(payload.get("mash_owner_id"), factory)
        fermentation_owner = self._assignee(payload.get("fermentation_owner_id"), factory)
        if not mash_owner or not fermentation_owner:
            raise ApiError("VALIDATION_REQUIRED", "请分别指定糖化负责人和发酵负责人。", 422)
        demand = self.env["brew.product.demand"].sudo().search([("id", "=", int(payload.get("demand_id") or 0)), ("company_id", "=", self.company.id), ("state", "=", "confirmed")], limit=1) if payload.get("demand_id") else self.env["brew.product.demand"]
        if payload.get("demand_id") and not demand:
            raise ApiError("STATE_PRECONDITION_FAILED", "来源需求不存在或尚未确认。", 422)
        if demand:
            self.env.cr.execute("SELECT id FROM brew_product_demand WHERE id = %s FOR UPDATE", [demand.id])
            demand.invalidate_recordset()
            if demand.deleted_at or demand.state != 'confirmed':
                raise ApiError("STATE_PRECONDITION_FAILED", "来源需求已关闭或已排产，请刷新。", 409)
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
        if payload.get("packaging_plan"):
            plan.with_context(brew_state_transition=True).write(self._packaging_values(payload, factory, plan.product_tmpl_id, plan.planned_volume_l, plan.planned_end, plan.demand_id))
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
        previous_product_id = plan.product_tmpl_id.id
        plan.write(values)
        if plan.product_tmpl_id.id != previous_product_id:
            self.env["brew.production.batch"].sudo().search([
                ("production_plan_id", "=", plan.id), ("state", "=", "draft")])._assign_product_identifiers(force=True)
        if plan.packaging_owner_id:
            packaging_values = self._packaging_values({"packaging_plan": json.loads(plan.packaging_plan_json or "{}"), "packaging_owner_id": plan.packaging_owner_id.id}, plan.factory_id, plan.product_tmpl_id, plan.planned_volume_l, plan.planned_end, plan.demand_id)
            plan.with_context(brew_state_transition=True).write(packaging_values)
            plan.ensure_execution_tasks()
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
        if plan.deleted_at:
            raise ApiError("STATE_PRECONDITION_FAILED", "生产计划已删除。", 409)
        if action in {"cancel", "delete", "terminate"}:
            require_close(self.user)
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                raise ApiError("VALIDATION_REQUIRED", "请填写取消或删除原因。", 422)
            with self.env.cr.savepoint():
                close_plan(self.env, plan, self.user, reason, delete=action == "delete")
            return plan, self.serialize(plan, True)
        if action == "approve":
            self._require_approve()
        else:
            self._require_manage()
        if action == "check-resources":
            result = plan.check_resources()
            return plan, {**self.serialize(plan, True), "check_result": result}
        if action == "set-raw-customer":
            if plan.state != 'done':
                raise ApiError("STATE_PRECONDITION_FAILED", "请在酿造完成后关联原液客户。", 422)
            customer_id = payload.get('customer_id')
            customer = self.env['res.partner'].sudo().search([('id','=',int(customer_id or 0)),('company_id','in',[False,self.company.id]),('customer_rank','>',0),('active','=',True)],limit=1)
            if customer_id and not customer:
                raise ApiError("DATA_SCOPE_DENIED", "客户不属于当前企业或已停用。", 403)
            data = json.loads(plan.packaging_plan_json or '{}')
            data.update(customer_id=str(customer.id) if customer else None, customer_name=customer.name if customer else None,
                        customer_reference=str(payload.get('customer_reference') or '').strip(), customer_updated_by=self.user.name,
                        customer_updated_at=fields.Datetime.now().isoformat())
            plan.with_context(brew_state_transition=True).write({'packaging_plan_json':json.dumps(data,ensure_ascii=False)})
            return plan, self.serialize(plan,True)
        if action == "confirm-raw-location":
            if plan.state != "done" or payload.get("confirmed") is not True:
                raise ApiError("STATE_PRECONDITION_FAILED", "请先核对酿造完成的原液及实际罐位。", 422)
            from .stock_browser import StockBrowserService
            rows = StockBrowserService(self.env, self.user, self.company, enforce_access=False).production_liquids({'factory_id': plan.factory_id.id, '_plan_id': plan.id})
            volume = sum(row['volume_l'] for row in rows)
            batch = self.env['brew.production.batch'].sudo().search([('production_plan_id', '=', plan.id)], limit=1)
            tank = self.env['brew.tank'].sudo().search([('id', '=', int(payload.get('tank_id') or 0)), ('factory_id', '=', plan.factory_id.id)], limit=1)
            if not batch or not tank or volume <= 0:
                raise ApiError("VALIDATION_RELATION", "请选择本厂罐位，并确认本批仍有剩余原液。", 422)
            self.env.cr.execute("SELECT id FROM brew_tank WHERE id = %s FOR UPDATE", [tank.id])
            tank.invalidate_recordset()
            linked = self.env['brew.tank'].sudo().search([('current_production_batch_id', '=', batch.id), ('current_volume_l', '>', 0)])
            if linked and tank not in linked:
                raise ApiError("STATE_PRECONDITION_FAILED", "本批已登记在其他罐中；真实转罐应办理转罐操作。", 422)
            if tank.current_volume_l > 0 and tank.current_production_batch_id != batch or tank.current_production_batch_id and tank.current_production_batch_id != batch:
                raise ApiError("STATE_PRECONDITION_FAILED", "该罐已被其他酒液占用。", 422)
            if volume > tank.capacity_l + 1e-8:
                raise ApiError("VALIDATION_RANGE", "剩余原液量超过所选罐容量。", 422)
            session = self.env['brew.fermentation.session'].sudo().search([('batch_id', '=', batch.id)], limit=1)
            if not session:
                session = self.env['brew.fermentation.session'].sudo().create_from_batch(batch)
                batch.fermentation_session_id = session
            session.write({'tank_id': tank.id, 'current_volume_l': volume, 'state': 'ready', 'completed_at': False})
            tank.write({'current_production_batch_id': batch.id, 'current_batch_no': batch.name,
                'current_product': plan.product_tmpl_id.name, 'current_volume_l': volume, 'status': 'ready'})
            completion = json.loads(plan.brewing_completion_json or '{}')
            completion.update(disposition='in_tank', destination=tank.name, location_confirmed_at=fields.Datetime.now().isoformat(), location_confirmed_by=self.user.name)
            plan.with_context(brew_state_transition=True).write({'brewing_completion_json': json.dumps(completion, ensure_ascii=False)})
            return plan, self.serialize(plan, True)
        if action == "set-packaging-plan":
            if plan.state not in {"draft", "checked", "stocking", "pending_approval", "approved", "done"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "请在任务下发前或酿造完成后安排包装。", 422)
            if json.loads(plan.packaging_plan_json or "{}").get("status") == "packaged":
                raise ApiError("STATE_PRECONDITION_FAILED", "该批次已确认包装完成，不能重新制定包装计划。", 422)
            if plan.state in {"cancelled", "terminated"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "已关闭计划不能安排包装。", 422)
            task = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "packaging"), ("state", "!=", "cancelled")], order="id desc", limit=1)
            if task:
                self.env.cr.execute("SELECT id FROM brew_task WHERE id = %s FOR UPDATE", [task.id])
                task.invalidate_recordset()
                completed_output = task.state == 'done' and self.env['brew.packaging.order'].sudo().search_count([('task_id', '=', task.id), ('state', 'in', ['pending_release', 'released'])])
                if task.state not in {"pending", "claimed"} and not completed_output:
                    raise ApiError("STATE_PRECONDITION_FAILED", "包装任务已开始或已完成，不能修改包装计划。", 422)
            volume = plan.planned_volume_l
            brewing_end = plan.planned_end
            if plan.state == 'done':
                from .stock_browser import StockBrowserService
                rows = StockBrowserService(self.env,self.user,self.company,enforce_access=False).production_liquids({'factory_id':plan.factory_id.id,'_plan_id':plan.id})
                volume = sum(r['volume_l'] for r in rows)
                completion = json.loads(plan.brewing_completion_json or '{}')
                ended = completion.get('confirmed_at') or completion.get('completed_at')
                if ended:
                    brewing_end = self._plan_datetime(ended,'酿造完成时间')
                if volume <= 0:
                    raise ApiError("STATE_PRECONDITION_FAILED", "本批没有可追加包装的剩余原液。", 422)
            values = self._packaging_values(payload, plan.factory_id, plan.product_tmpl_id, volume, brewing_end, plan.demand_id)
            before = json.loads(plan.packaging_plan_json or '{}')
            after = json.loads(values['packaging_plan_json'])
            after.update({key:value for key,value in before.items() if key.startswith('customer_')})
            values['packaging_plan_json'] = json.dumps(after,ensure_ascii=False)
            plan.with_context(brew_state_transition=True).write(values)
            if not plan.packaging_owner_id and task:
                task.write({"state": "cancelled"})
            plan.ensure_execution_tasks()
            return plan, self.serialize(plan, True)
        if action == "expand-materials":
            result = plan.expand_materials()
            return plan, {**self.serialize(plan, True), "calculation": result}
        if action == "request-stock": self.request_stock(plan)
        elif action == "complete-stock":
            if plan.state != "stocking":
                raise ApiError("STATE_PRECONDITION_FAILED", "当前计划不在备货阶段。", 422)
            self._dispatch_directly(plan)
        elif action in {"submit", "dispatch"}:
            self._dispatch_directly(plan)
        elif action == "approve": plan.action_approve()
        elif action == "cancel": plan.action_cancel(payload.get("reason"))
        elif action == "terminate": plan.action_terminate(payload.get("reason"))
        elif action == "complete-brewing": self._complete_brewing(plan, payload)
        else: raise ApiError("RESOURCE_NOT_FOUND", "生产计划动作不存在。", 404)
        return plan, self.serialize(plan, True)

    def _complete_brewing(self, plan, payload):
        self._require_manage()
        if plan.state not in {"dispatched", "in_progress"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有执行中的生产计划可以确认酿酒结束。", 422)
        if payload.get("confirmed") is not True:
            raise ApiError("VALIDATION_REQUIRED", "请确认已核对产量、损耗和酒液去向。", 422)
        tasks = self.env["brew.task"].sudo().search([
            ("target_model", "=", plan._name), ("target_id", "=", plan.id),
            ("task_type", "in", ["mashing", "fermentation"]), ("state", "!=", "cancelled"),
        ])
        if set(tasks.mapped("task_type")) != {"mashing", "fermentation"} or any(task.state != "done" for task in tasks):
            raise ApiError("STATE_PRECONDITION_FAILED", "请先由负责人完成糖化任务和发酵任务。", 422)
        amounts = {}
        for key, label in [("actual_volume_l", "实际产酒量"), ("loss_volume_l", "损耗量")]:
            value = payload.get(key)
            try:
                if value is None or isinstance(value, bool) or value == "": raise ValueError()
                value = float(value)
                if not math.isfinite(value) or value < 0 or (key == "actual_volume_l" and value == 0): raise ValueError()
            except (TypeError, ValueError):
                raise ApiError("VALIDATION_RANGE", f"请填写有效的{label}。", 422)
            amounts[key] = value
        disposition = payload.get("disposition")
        destination = str(payload.get("destination") or "").strip()
        if disposition not in {"in_tank", "emptied"} or not destination:
            raise ApiError("VALIDATION_REQUIRED", "请选择酒液状态并填写酒液去向。", 422)
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
        if not batch or batch.state == "cancelled":
            raise ApiError("STATE_PRECONDITION_FAILED", "当前计划没有可完成的生产批次。", 422)
        sessions = self.env["brew.fermentation.session"].sudo().search([("batch_id", "=", batch.id), ("state", "not in", ["completed", "terminated"])])
        if len(sessions) > 1:
            raise ApiError("STATE_PRECONDITION_FAILED", "存在多个未结束的发酵会话，请先核对罐体。", 422)
        if sessions.node_ids.filtered(lambda node: node.mandatory_confirmation and node.action_type != "packaging_release" and node.state != "completed"):
            raise ApiError("STATE_PRECONDITION_FAILED", "仍有必须确认的发酵工艺节点未完成。", 422)
        tank = sessions.tank_id if sessions else plan.tank_id
        self.env.cr.execute("SELECT id FROM brew_tank WHERE id = %s FOR UPDATE", [tank.id])
        tank.invalidate_recordset()
        if tank.current_production_batch_id and tank.current_production_batch_id != batch:
            raise ApiError("STATE_PRECONDITION_FAILED", "罐体已被其他批次占用，请核对后重试。", 422)
        if not tank.current_production_batch_id and (tank.current_volume_l > 0 or tank.status not in {"empty", "ready"}):
            raise ApiError("STATE_PRECONDITION_FAILED", "罐体占用信息与当前批次不一致，请先核对。", 422)
        if disposition == "in_tank" and amounts["actual_volume_l"] > tank.capacity_l:
            raise ApiError("VALIDATION_RANGE", "在罐酒液量不能超过罐体容量。", 422)
        now = fields.Datetime.now()
        completion = {**amounts, "disposition": disposition, "destination": destination,
                      "tank": {"id": str(tank.id), "name": tank.name},
                      "note": str(payload.get("note") or "").strip(),
                      "confirmed_at": now.isoformat(), "confirmed_by": self.user.name,
                      "confirmed_by_id": str(self.user.id)}
        batch.write({"state": "completed", "actual_volume_l": amounts["actual_volume_l"], "completed_at": now})
        if disposition == "in_tank":
            tank.write({"status": "ready", "current_product": plan.product_tmpl_id.display_name,
                        "current_batch_no": batch.name, "current_volume_l": amounts["actual_volume_l"],
                        "current_production_batch_id": batch.id})
            sessions.write({"state": "ready", "current_volume_l": amounts["actual_volume_l"]})
        else:
            tank.write({"status": "empty", "cleaning_status": "dirty", "current_product": False,
                        "current_batch_no": False, "current_volume_l": 0, "current_production_batch_id": False})
            sessions.write({"state": "completed", "completed_at": now})
        plan.with_context(brew_state_transition=True).write({"state": "done", "brewing_completion_json": json.dumps(completion, ensure_ascii=False)})

    def _auto_dispatch_stocked(self, factory_id):
        """Trusted scheduler/stock-posting hook; never called by a read endpoint."""
        self._require_manage()
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(873021, %s)", [factory_id])
        if not self.env.cr.fetchone()[0]:
            return []
        plans = self.env["brew.production.plan"].sudo().search([
            ("company_id", "=", self.company.id), ("factory_id", "=", factory_id),
            ("state", "=", "stocking"),
        ], order="planned_start, id")
        dispatched = []
        for plan in plans:
            self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE SKIP LOCKED", [plan.id])
            if not self.env.cr.fetchone():
                continue
            plan.invalidate_recordset()
            if plan.state != "stocking":
                continue
            try:
                with self.env.cr.savepoint():
                    inventory = InventoryService(self.env, self.user, self.company).plan_inventory(plan.id, reserve=False)
                    if not inventory["materials"] or not inventory["ok"]:
                        self._stock_dispatch_status(plan, "waiting_stock")
                        continue
                    self._dispatch_directly(plan)
                    self._stock_dispatch_status(plan, "dispatched")
                    self.env["brew.outbox.event"].sudo().enqueue(
                        "production_plan.dispatch", plan._name, plan.id,
                        {"plan_id": plan.id, "name": plan.name, "state": plan.state, "automatic": True}, self.company)
                    self.env["brew.audit.log"].sudo().log_event(
                        "production_plan", "auto_dispatch", user=self.user, company=self.company,
                        resource_model=plan._name, resource_id=str(plan.id),
                        summary={"plan_id": plan.id, "reason": "原料备齐后自动下发"})
                    dispatched.append(plan.id)
            except TransactionRollbackError:
                raise
            except (ApiError, ValidationError) as exc:
                self._stock_dispatch_status(plan, "blocked", str(exc), getattr(exc, "code", "VALIDATION_FAILED"))
            except Exception:
                _logger.exception("Automatic dispatch failed for production plan %s", plan.id)
                self._stock_dispatch_status(plan, "blocked", "自动下发暂未完成，系统将自动重试。", "AUTO_DISPATCH_FAILED")
        return dispatched

    def _stock_dispatch_status(self, plan, state, reason="", code=""):
        request = json.loads(plan.stocking_request_json or "{}")
        previous = request.get("auto_dispatch") or {}
        if (previous.get("state"), previous.get("reason", ""), previous.get("code", "")) == (state, reason, code):
            return
        request["auto_dispatch"] = {"state": state, "reason": reason, "code": code,
                                    "checked_at": fields.Datetime.now().isoformat()}
        plan.with_context(brew_state_transition=True).write({"stocking_request_json": json.dumps(request, ensure_ascii=False)})

    def _dispatch_directly(self, plan):
        # Validate and reserve in the same transaction as dispatch. Legacy
        # pending approvals are retired only after all checks succeed.
        self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
        plan.invalidate_recordset()
        if plan.state not in {"draft", "checked", "stocking", "pending_approval", "approved"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有未下发的计划可以下发。", 422)
        if not plan.mash_owner_id or not plan.fermentation_owner_id:
            raise ApiError("VALIDATION_REQUIRED", "请先指定糖化和发酵负责人。", 422)
        if plan.state in {"stocking", "pending_approval", "approved"}:
            plan.with_context(brew_state_transition=True).write({"state": "draft"})
        result = plan.check_resources()
        if not result["ok"]:
            names = "、".join(item["name"] for item in result["conflicts"])
            raise ApiError("RESOURCE_CONFLICT", f"生产安排存在冲突：{names}。请调整生产时间、设备或罐体后重新下发。", 409, result)
        InventoryService(self.env, self.user, self.company).plan_inventory(plan.id, reserve=True)
        request = json.loads(plan.stocking_request_json or "{}")
        if request.get("submitted_at"):
            request["completed_at"] = fields.Datetime.now().isoformat()
            plan.with_context(brew_state_transition=True).write({"stocking_request_json": json.dumps(request, ensure_ascii=False)})
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
        # Procurement collects these plan shortages and creates requisitions there.
        request = {"source": "production_shortages", "submitted_at": fields.Datetime.now().isoformat(), "submitted_by": self.user.name, "materials": shortages}
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
        completion = json.loads(plan.brewing_completion_json or "{}")
        if completion.get("confirmed_at"):
            timeline.append({"at": completion["confirmed_at"], "title": "确认酿酒结束", "by": completion["confirmed_by"], "value": f"产酒 {completion['actual_volume_l']} L · 损耗 {completion['loss_volume_l']} L · {completion['destination']}"})
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
        can_issue = self.user.has_group("craft_brew_core.group_brew_warehouse") or self.user.has_group("craft_brew_core.group_brew_admin")
        locations = self.env["stock.location"].sudo().search([("company_id", "=", self.company.id), ("brew_factory_id", "=", plan.factory_id.id), ("usage", "=", "internal")]) if can_issue else self.env["stock.location"]
        from .production import ProductionService
        production = ProductionService(self.env, self.user, self.company)
        requisitions = self.env["brew.material.requisition"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")])
        can_settle = plan.mash_owner_id == self.user or self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin")
        material_state = "waiting_issue"
        if requisitions:
            states = set(requisitions.mapped("state"))
            material_state = "completed" if states == {"completed"} else "acknowledged" if states <= {"acknowledged", "completed"} else "issued" if states <= {"issued", "acknowledged", "completed"} else "recommended" if states == {"recommended"} else "waiting_issue"
        material_flow = {"state": material_state, "owner": {"id": str(plan.mash_owner_id.id), "name": plan.mash_owner_id.name} if plan.mash_owner_id else None}
        stocking_inventory = None
        if plan.state == "stocking":
            stocking_inventory = InventoryService(self.env, self.user, self.company, enforce_access=False).plan_inventory(plan.id, reserve=False)
        from .stock_browser import StockBrowserService
        stock = StockBrowserService(self.env, self.user, self.company, enforce_access=False)
        liquids = stock.production_liquids({'factory_id': str(plan.factory_id.id), '_plan_id': plan.id})
        packaging_orders = self.env['brew.packaging.order'].sudo().search([('company_id', '=', self.company.id), ('batch_id', '=', batch.id)]) if batch else self.env['brew.packaging.order']
        packaging_history = []
        for order in packaging_orders:
            quants = stock.quants(plan.factory_id.id, order.product_id.id).filtered(lambda q: q.lot_id == order.finished_lot_id) if order.finished_lot_id else self.env['stock.quant']
            reservations = stock._reservation_totals(quants.mapped('location_id').ids, order.product_id.id) if quants else {}
            packaging_history.append({'id': str(order.id), 'name': order.name, 'state': order.state, 'specification': order.packaging_id.name,
                'quantity': order.output_qty, 'volume_l': order.output_volume_l, 'loss_l': order.loss_volume_l,
                'submitted_at': order.submitted_at.isoformat() if order.submitted_at else None,
                'released_at': order.released_at.isoformat() if order.released_at else None,
                'lot': order.finished_lot_id.name or None, 'location': order.finished_location_id.complete_name,
                'unit': stock.product_info(order.product_id)['unit'], **stock.balances(quants, reservations)})
        journey = {'batch': {'id': str(batch.id), 'name': batch.name, 'state': batch.state, 'finished_product_code': batch.finished_product_code,
            'entered_tank_at': batch.entered_tank_at.isoformat() if batch.entered_tank_at else None,
            'actual_volume_l': batch.actual_volume_l, 'completed_at': batch.completed_at.isoformat() if batch.completed_at else None} if batch else None,
            'liquid': liquids[0] if liquids else None, 'packaging_orders': packaging_history}
        from .packaging_execution import execution_options
        packaging_execution_options = execution_options(self.env, self.company, plan)
        return {"packaging_execution_options": packaging_execution_options, "raw_customers": [{"id":str(c.id),"name":c.name} for c in self.env['res.partner'].sudo().search([('company_id','in',[False,self.company.id]),('customer_rank','>',0),('active','=',True)],order='name',limit=500)] if plan.state == 'done' and (self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin")) else [], "journey": journey, "material_flow": material_flow, "stocking_inventory": stocking_inventory, "plan": self.serialize(plan, True), "tasks": [workflow.serialize_task(task) for task in tasks], "timeline": timeline, "can_manage": self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin"), "can_issue": can_issue, "can_settle_materials": bool(can_settle), "material_requisitions": [production._serialize_requisition(record) for record in requisitions], "material_locations": [{"id": str(location.id), "name": location.complete_name, "parent_id": str(location.location_id.id) if location.location_id else None, "type": location.brew_location_type} for location in locations], "approval": {"id": str(plan.approval_instance_id.id), "state": plan.approval_instance_id.state} if plan.approval_instance_id else None}
