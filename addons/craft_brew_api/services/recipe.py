import json

from odoo import Command

from ..controllers.base import ApiError
from .recipe_design import design_values


class RecipeService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company

    def ensure_manage(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权维护配方版本。", 403)

    def _recipe(self, recipe_id, include_archived=True):
        recipe = self.env["brew.recipe"].sudo().with_context(active_test=not include_archived).search(
            [("id", "=", int(recipe_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not recipe:
            raise ApiError("RESOURCE_NOT_FOUND", "配方不存在。", 404)
        return recipe

    def _version(self, version_id):
        version = self.env["brew.recipe.version"].sudo().search(
            [("id", "=", int(version_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not version:
            raise ApiError("RESOURCE_NOT_FOUND", "配方版本不存在。", 404)
        return version

    def _check_version(self, record, expected):
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})

    def serialize_recipe(self, recipe):
        return {
            "id": str(recipe.id),
            "code": recipe.code,
            "name": recipe.name,
            "product": {"id": str(recipe.product_tmpl_id.id), "name": recipe.product_tmpl_id.display_name} if recipe.product_tmpl_id else None,
            "beer_style": recipe.beer_style,
            "beer_category": {"id": str(recipe.beer_category_id.id), "name": recipe.beer_category_id.name} if recipe.beer_category_id else None,
            "beer_subcategory": {"id": str(recipe.beer_subcategory_id.id), "name": recipe.beer_subcategory_id.name} if recipe.beer_subcategory_id else None,
            "description": recipe.description,
            "active_version_id": str(recipe.active_version_id.id) if recipe.active_version_id else None,
            "active": recipe.active,
            "version": recipe.write_date.isoformat() if recipe.write_date else None,
        }

    def serialize_version(self, version, detail=False):
        data = {
            "id": str(version.id),
            "recipe_id": str(version.recipe_id.id),
            "name": version.name,
            "version": version.version,
            "state": version.state,
            "base_volume_l": version.base_volume_l,
            "original_gravity": version.original_gravity,
            "final_gravity": version.final_gravity,
            "bitterness_ibu": version.bitterness_ibu,
            "color_ebc": version.color_ebc,
            "alcohol_abv": version.alcohol_abv,
            "estimated_cost": version.estimated_cost,
            "currency": version.currency_id.name,
            "notes": version.notes,
            "design": json.loads(version.design_json or "{}"),
            "return_reason": version.return_reason,
            "submitted_at": version.submitted_at.isoformat() if version.submitted_at else None,
            "approved_at": version.approved_at.isoformat() if version.approved_at else None,
            "record_version": version.write_date.isoformat() if version.write_date else None,
        }
        if detail:
            data["materials"] = [self.serialize_material(line) for line in version.material_line_ids.sorted("sequence")]
            data["process_steps"] = [self.serialize_step(step) for step in version.process_step_ids.sorted("sequence")]
        return data

    def serialize_material(self, line):
        return {
            "id": str(line.id),
            "sequence": line.sequence,
            "product_id": str(line.product_id.id),
            "product_name": line.product_id.display_name,
            "quantity": line.quantity,
            "uom_id": str(line.uom_id.id),
            "uom_name": line.uom_id.name,
            "stage": line.stage,
            "notes": line.notes,
        }

    def serialize_step(self, step):
        return {
            "id": str(step.id),
            "sequence": step.sequence,
            "name": step.name,
            "phase": step.phase,
            "action_type": step.action_type,
            "mandatory_confirmation": step.mandatory_confirmation,
            "target_duration_hours": step.target_duration_hours,
            "target_temperature": step.target_temperature,
            "config": json.loads(step.config_json or "{}"),
        }

    def list_recipes(self, page, page_size, keyword="", active=None):
        model = self.env["brew.recipe"].sudo().with_context(active_test=False)
        domain = [("company_id", "=", self.company.id)]
        if keyword:
            domain += ["|", ("code", "ilike", keyword), ("name", "ilike", keyword)]
        if active is not None:
            domain += [("active", "=", active)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="code,id")
        return [self.serialize_recipe(item) for item in records], total

    def create_recipe(self, payload):
        self.ensure_manage()
        name = str(payload.get("name") or "").strip()
        code = str(payload.get("code") or "").strip()
        product_id = payload.get("product_tmpl_id")
        product = self.env["product.template"].sudo().search(
            [("id", "=", int(product_id or 0)), ("company_id", "=", self.company.id), ("brew_item_type", "=", "finished_product")],
            limit=1,
        ) if product_id else False
        if not name:
            raise ApiError("VALIDATION_REQUIRED", "配方名称不能为空。", 422)
        if not code:
            prefix = "".join(ch for ch in (product.name if product else name) if ch.isalnum())[:3].upper() or "BEER"
            code = f"{prefix}-{product.id if product else 'RCP'}-1-1"
            suffix = 1
            while self.env["brew.recipe"].sudo().search_count([("company_id", "=", self.company.id), ("code", "=", code)]):
                suffix += 1
                code = f"{prefix}-{product.id if product else 'RCP'}-1-{suffix}"
        category_id = int(payload.get("beer_category_id") or 0)
        subcategory_id = int(payload.get("beer_subcategory_id") or 0)
        category_model = self.env["brew.beer.category"].sudo()
        category = category_model.search([("id", "=", category_id), ("active", "=", True)], limit=1) if category_id else False
        subcategory = category_model.search([("id", "=", subcategory_id), ("active", "=", True)], limit=1) if subcategory_id else False
        if not category or not subcategory or subcategory.parent_id != category:
            raise ApiError("VALIDATION_REQUIRED", "请选择有效的啤酒大类和对应小类。", 422)
        recipe = self.env["brew.recipe"].sudo().create(
            {"name": name, "code": code, "company_id": self.company.id, "product_tmpl_id": product.id if product else False, "beer_style": payload.get("beer_style"), "beer_category_id": category.id, "beer_subcategory_id": subcategory.id, "description": payload.get("description")}
        )
        data = self.serialize_recipe(recipe)
        if "initial_version" in payload:
            _version, data["initial_version"] = self.create_version(recipe.id, payload["initial_version"])
        return recipe, data

    def update_recipe(self, recipe_id, payload):
        self.ensure_manage()
        recipe = self._recipe(recipe_id)
        self._check_version(recipe, payload.get("version"))
        values = {key: payload[key] for key in {"name", "beer_style", "description"} if key in payload}
        if "beer_category_id" in payload or "beer_subcategory_id" in payload:
            category = self.env["brew.beer.category"].sudo().browse(int(payload.get("beer_category_id") or recipe.beer_category_id.id))
            subcategory = self.env["brew.beer.category"].sudo().browse(int(payload.get("beer_subcategory_id") or recipe.beer_subcategory_id.id))
            if not category.exists() or not subcategory.exists() or subcategory.parent_id != category:
                raise ApiError("VALIDATION_REQUIRED", "请选择有效的啤酒大类和对应小类。", 422)
            values.update({"beer_category_id": category.id, "beer_subcategory_id": subcategory.id})
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        recipe.write(values)
        return recipe, self.serialize_recipe(recipe)

    def list_versions(self, recipe_id):
        recipe = self._recipe(recipe_id)
        return [self.serialize_version(item) for item in recipe.version_ids.sorted(lambda item: (-item.version, item.id))]

    def create_version(self, recipe_id, payload):
        self.ensure_manage()
        recipe = self._recipe(recipe_id)
        next_version = max(recipe.version_ids.mapped("version") or [0]) + 1
        detail_values = design_values(self.env, self.company, payload.get("design") or {}, payload.get("base_volume_l") or 1000)
        version = self.env["brew.recipe.version"].sudo().create(
            {
                "recipe_id": recipe.id,
                **detail_values,
                "version": next_version,
                "base_volume_l": float(payload.get("base_volume_l") or 1000),
                "original_gravity": payload.get("original_gravity"),
                "final_gravity": payload.get("final_gravity"),
                "bitterness_ibu": payload.get("bitterness_ibu"),
                "color_ebc": payload.get("color_ebc"),
                "alcohol_abv": payload.get("alcohol_abv"),
                "notes": payload.get("notes"),
                "design_json": json.dumps(payload.get("design") or {}, ensure_ascii=False, sort_keys=True),
            }
        )
        return version, self.serialize_version(version, True)

    def update_version(self, version_id, payload):
        self.ensure_manage()
        version = self._version(version_id)
        self._check_version(version, payload.get("record_version"))
        values = {key: payload[key] for key in {"base_volume_l", "original_gravity", "final_gravity", "bitterness_ibu", "color_ebc", "alcohol_abv", "notes"} if key in payload}
        if "design" in payload:
            values["design_json"] = json.dumps(payload.get("design") or {}, ensure_ascii=False, sort_keys=True)
            values.update(design_values(self.env, self.company, payload.get("design") or {}, payload.get("base_volume_l", version.base_volume_l)))
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        version.write(values)
        return version, self.serialize_version(version, True)

    def replace_materials(self, version_id, payload):
        self.ensure_manage()
        version = self._version(version_id)
        self._check_version(version, payload.get("record_version"))
        lines = payload.get("materials")
        if not isinstance(lines, list) or not lines:
            raise ApiError("VALIDATION_REQUIRED", "原料明细不能为空。", 422)
        commands = [Command.clear()]
        for index, item in enumerate(lines, start=1):
            product_id = int(item.get("product_id") or 0)
            product = self.env["product.product"].sudo().search(
                [("id", "=", product_id), ("product_tmpl_id.company_id", "=", self.company.id), ("product_tmpl_id.brew_item_type", "in", ["raw_material", "packaging_material"])],
                limit=1,
            )
            if not product:
                template = self.env["product.template"].sudo().search(
                    [("id", "=", product_id), ("company_id", "=", self.company.id), ("brew_item_type", "in", ["raw_material", "packaging_material"])],
                    limit=1,
                )
                product = template.product_variant_id if template else product
            if not product:
                raise ApiError("DATA_SCOPE_DENIED", "原料不属于当前企业。", 403)
            commands.append(Command.create({"sequence": int(item.get("sequence") or index * 10), "product_id": product.id, "quantity": float(item.get("quantity") or 0), "uom_id": int(item.get("uom_id") or product.uom_id.id), "stage": item.get("stage") or "mash", "notes": item.get("notes")}))
        version.write({"material_line_ids": commands})
        return version, self.serialize_version(version, True)

    def replace_steps(self, version_id, payload):
        self.ensure_manage()
        version = self._version(version_id)
        self._check_version(version, payload.get("record_version"))
        steps = payload.get("process_steps")
        if not isinstance(steps, list) or not steps:
            raise ApiError("VALIDATION_REQUIRED", "工艺节点不能为空。", 422)
        commands = [Command.clear()]
        for index, item in enumerate(steps, start=1):
            config = item.get("config") or {}
            if not isinstance(config, dict):
                raise ApiError("VALIDATION_FORMAT", "工艺节点配置必须是 JSON 对象。", 422)
            commands.append(Command.create({"sequence": int(item.get("sequence") or index * 10), "name": str(item.get("name") or "").strip(), "phase": item.get("phase"), "action_type": item.get("action_type") or "record", "mandatory_confirmation": bool(item.get("mandatory_confirmation")), "target_duration_hours": item.get("target_duration_hours"), "target_temperature": item.get("target_temperature"), "config_json": json.dumps(config, ensure_ascii=False, sort_keys=True)}))
        version.write({"process_step_ids": commands})
        return version, self.serialize_version(version, True)

    def copy_version(self, version_id):
        self.ensure_manage()
        copied = self._version(version_id).copy_as_draft()
        return copied, self.serialize_version(copied, True)

    def calculate(self, version_id, volume_l):
        return self._version(version_id).calculate(float(volume_l or 0))

    def action(self, version_id, action, payload):
        self.ensure_manage()
        version = self._version(version_id)
        self._check_version(version, payload.get("record_version"))
        if action == "submit":
            version.action_submit(self.user)
        elif action == "approve":
            version.action_approve(self.user)
        elif action == "return":
            version.action_return(payload.get("reason"))
        elif action == "activate":
            version.action_activate()
        elif action == "deactivate":
            version.action_deactivate()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "配方版本动作不存在。", 404)
        return version, self.serialize_version(version, True)

    def compare(self, version_id, other_id):
        left = self._version(version_id)
        right = self._version(other_id)
        def keyed_materials(version):
            return {str(line.product_id.id): self.serialize_material(line) for line in version.material_line_ids}
        def keyed_steps(version):
            return {f"{step.sequence}:{step.action_type}": self.serialize_step(step) for step in version.process_step_ids}
        return {"left": self.serialize_version(left), "right": self.serialize_version(right), "materials": {"left": keyed_materials(left), "right": keyed_materials(right)}, "process_steps": {"left": keyed_steps(left), "right": keyed_steps(right)}}
