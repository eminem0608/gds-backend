import json

from odoo import Command, fields

from ..controllers.base import ApiError
from .recipe_design import design_values


class RecipeService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company

    def ensure_manage(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_brewer")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "只有酿酒师或系统管理员可以新增或修改配方工艺。", 403)

    def ensure_review(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权审核配方工艺。", 403)

    def _recipe(self, recipe_id, include_archived=True):
        recipe = self.env["brew.recipe"].sudo().with_context(active_test=not include_archived).search(
            [("id", "=", int(recipe_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not recipe:
            raise ApiError("RESOURCE_NOT_FOUND", "配方不存在。", 404)
        self._ensure_factory_scope(recipe.equipment_id.brew_factory_id)
        return recipe

    def _ensure_factory_scope(self, factory):
        if factory and factory not in self.user.brew_allowed_factories(self.company):
            raise ApiError("DATA_SCOPE_DENIED", "工艺不属于当前厂区范围。", 403)

    def _version(self, version_id):
        version = self.env["brew.recipe.version"].sudo().search(
            [("id", "=", int(version_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not version:
            raise ApiError("RESOURCE_NOT_FOUND", "配方版本不存在。", 404)
        self._ensure_factory_scope(version.recipe_id.equipment_id.brew_factory_id)
        return version

    def _check_version(self, record, expected):
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})

    def serialize_recipe(self, recipe):
        latest_version = recipe.version_ids.sorted("version", reverse=True)[:1]
        return {
            "id": str(recipe.id),
            "code": recipe.code,
            "name": recipe.name,
            "product": {"id": str(recipe.product_tmpl_id.id), "name": recipe.product_tmpl_id.display_name} if recipe.product_tmpl_id else None,
            "equipment": {"id": str(recipe.equipment_id.id), "name": recipe.equipment_id.display_name, "brew_capacity_l": recipe.equipment_id.brew_capacity_l} if recipe.equipment_id else None,
            "active_version_number": recipe.active_version_id.version if recipe.active_version_id else None,
            "active_version_label": f"基础工艺 V{recipe.active_version_id.version:02d}" if recipe.active_version_id else None,
            "active_version_base_volume_l": recipe.active_version_id.base_volume_l if recipe.active_version_id else None,
            "beer_style": recipe.beer_style,
            "beer_category": {"id": str(recipe.beer_category_id.id), "name": recipe.beer_category_id.name} if recipe.beer_category_id else None,
            "beer_subcategory": {"id": str(recipe.beer_subcategory_id.id), "name": recipe.beer_subcategory_id.name} if recipe.beer_subcategory_id else None,
            "description": recipe.description,
            "active_version_id": str(recipe.active_version_id.id) if recipe.active_version_id else None,
            "latest_version_id": str(latest_version.id) if latest_version else None,
            "latest_version_state": latest_version.state if latest_version else None,
            "active": recipe.active,
            "version": recipe.write_date.isoformat() if recipe.write_date else None,
        }

    def serialize_version(self, version, detail=False):
        data = {
            "id": str(version.id),
            "recipe_id": str(version.recipe_id.id),
            "name": version.name,
            "version": version.version,
            "version_type": "base",
            "version_label": f"基础工艺 V{version.version:02d}",
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
            data["review_history"] = self.review_history(version)
        return data

    def review_history(self, version):
        events = self.env["brew.recipe.review.event"].sudo().search(
            [("version_id", "=", version.id)], order="event_at,id"
        )
        if events:
            history = [
                {
                    "review_round": event.review_round,
                    "action": event.action,
                    "actor_name": event.user_id.name,
                    "at": event.event_at.isoformat() if event.event_at else None,
                    "comment": event.comment,
                }
                for event in events
            ]
            recorded_actions = {item["action"] for item in history}
            if version.submitted_at and "submitted" not in recorded_actions:
                history.append(
                    {
                        "review_round": 1,
                        "action": "submitted",
                        "actor_name": version.submitted_by_id.name if version.submitted_by_id else "未知用户",
                        "at": version.submitted_at.isoformat(),
                        "comment": None,
                    }
                )
            if version.approved_at and "approved" not in recorded_actions:
                history.append(
                    {
                        "review_round": 1,
                        "action": "approved",
                        "actor_name": version.approved_by_id.name if version.approved_by_id else "未知用户",
                        "at": version.approved_at.isoformat(),
                        "comment": None,
                    }
                )
            if version.state == "returned" and version.return_reason and "returned" not in recorded_actions:
                history.append(
                    {
                        "review_round": 1,
                        "action": "returned",
                        "actor_name": "审核人",
                        "at": None,
                        "comment": version.return_reason,
                    }
                )
            history.sort(key=lambda item: item["at"] or "")
            return history

        # Existing versions predate the review timeline. Preserve the submit and
        # approval facts already stored on the version until new events accrue.
        history = []
        if version.submitted_at:
            history.append(
                {
                    "review_round": 1,
                    "action": "submitted",
                    "actor_name": version.submitted_by_id.name if version.submitted_by_id else "未知用户",
                    "at": version.submitted_at.isoformat(),
                    "comment": None,
                }
            )
        if version.approved_at:
            history.append(
                {
                    "review_round": 1,
                    "action": "approved",
                    "actor_name": version.approved_by_id.name if version.approved_by_id else "未知用户",
                    "at": version.approved_at.isoformat(),
                    "comment": None,
                }
            )
        if version.state == "returned" and version.return_reason:
            history.append(
                {
                    "review_round": 1,
                    "action": "returned",
                    "actor_name": "审核人",
                    "at": None,
                    "comment": version.return_reason,
                }
            )
        return history

    def _create_recipe_review_tasks(self, version):
        event_model = self.env["brew.recipe.review.event"].sudo()
        submission = event_model.search(
            [("version_id", "=", version.id), ("action", "=", "submitted")],
            order="review_round desc,id desc",
            limit=1,
        )
        review_round = submission.review_round if submission else 1
        for role_code in ("production_director", "tenant_admin"):
            self.env["brew.task"].sudo().create(
                {
                    "name": f"配方审核 · {version.recipe_id.name} · {version.name} · 第 {review_round} 轮",
                    "task_type": "recipe_review",
                    "company_id": version.company_id.id,
                    "role_code": role_code,
                    "target_model": version._name,
                    "target_id": version.id,
                }
            )

    def _create_recipe_revision_task(self, version):
        submitter = version.submitted_by_id
        if not submitter:
            return
        task_model = self.env["brew.task"].sudo()
        active_task = task_model.search(
            [
                ("company_id", "=", version.company_id.id),
                ("task_type", "=", "recipe_revision"),
                ("target_model", "=", version._name),
                ("target_id", "=", version.id),
                ("state", "not in", ["done", "cancelled"]),
            ],
            limit=1,
        )
        if active_task:
            return
        submission = self.env["brew.recipe.review.event"].sudo().search(
            [("version_id", "=", version.id), ("action", "=", "submitted")],
            order="review_round desc,id desc",
            limit=1,
        )
        review_round = submission.review_round if submission else 1
        task_model.create(
            {
                "name": f"配方修改 · {version.recipe_id.name} · {version.name} · 第 {review_round} 轮退回",
                "task_type": "recipe_revision",
                "company_id": version.company_id.id,
                "assignee_id": submitter.id,
                "target_model": version._name,
                "target_id": version.id,
                "last_comment": version.return_reason,
            }
        )

    def _complete_recipe_revision_tasks(self, version):
        self.env["brew.task"].sudo().search(
            [
                ("company_id", "=", version.company_id.id),
                ("task_type", "=", "recipe_revision"),
                ("target_model", "=", version._name),
                ("target_id", "=", version.id),
                ("state", "not in", ["done", "cancelled"]),
            ]
        ).write(
            {
                "state": "done",
                "completed_at": fields.Datetime.now(),
                "last_comment": "已重新提交审核",
            }
        )

    def _complete_recipe_review_tasks(self, version, comment=None):
        tasks = self.env["brew.task"].sudo().search(
            [
                ("company_id", "=", version.company_id.id),
                ("task_type", "=", "recipe_review"),
                ("target_model", "=", version._name),
                ("target_id", "=", version.id),
                ("state", "not in", ["done", "cancelled"]),
            ]
        )
        tasks.write(
            {
                "state": "done",
                "completed_at": fields.Datetime.now(),
                "last_comment": comment,
            }
        )

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
        domain = [("company_id", "=", self.company.id), ("equipment_id.brew_factory_id", "in", self.user.brew_allowed_factories(self.company).ids)]
        if keyword:
            domain += ["|", ("code", "ilike", keyword), ("name", "ilike", keyword)]
        if active is not None:
            domain += [("active", "=", active)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="code,id")
        return [self.serialize_recipe(item) for item in records], total

    def create_recipe(self, payload):
        self.ensure_manage()
        code = str(payload.get("code") or "").strip()
        product_id = payload.get("product_tmpl_id")
        product = self.env["product.template"].sudo().search(
            [("id", "=", int(product_id or 0)), ("company_id", "=", self.company.id), ("brew_item_type", "=", "finished_product")],
            limit=1,
        ) if product_id else False
        if not product:
            raise ApiError("VALIDATION_REQUIRED", "请选择当前企业中有效的啤酒产品。", 422)
        equipment = self.env["maintenance.equipment"].sudo().search(
            [("id", "=", int(payload.get("equipment_id") or 0)), ("company_id", "=", self.company.id), ("brew_equipment_type", "=", "mash"), ("active", "=", True)],
            limit=1,
        )
        if not equipment:
            raise ApiError("VALIDATION_REQUIRED", "请选择当前企业中有效的酿造系统。", 422)
        self._ensure_factory_scope(equipment.brew_factory_id)
        recipe_model = self.env["brew.recipe"].sudo().with_context(active_test=False)
        if not code:
            prefix = "".join(ch for ch in product.name if ch.isalnum())[:3].upper() or "BEER"
            code = f"{prefix}-{product.id}-{equipment.id}-1"
            suffix = 1
            while recipe_model.search_count([("company_id", "=", self.company.id), ("code", "=", code)]):
                suffix += 1
                code = f"{prefix}-{product.id}-{equipment.id}-{suffix}"
        elif recipe_model.search_count([("company_id", "=", self.company.id), ("code", "=", code)]):
            raise ApiError("VALIDATION_DUPLICATE", "配方编码已存在（包括已归档配方），请使用其他编码。", 409)
        category_id = product.brew_beer_category_id.id
        subcategory_id = product.brew_beer_subcategory_id.id
        category_model = self.env["brew.beer.category"].sudo()
        category = category_model.search([("id", "=", category_id), ("active", "=", True)], limit=1) if category_id else False
        subcategory = category_model.search([("id", "=", subcategory_id), ("active", "=", True)], limit=1) if subcategory_id else False
        if not category or not subcategory or subcategory.parent_id != category:
            raise ApiError("VALIDATION_REQUIRED", "请先在啤酒产品中维护有效的大类和对应小类。", 422)
        recipe = self.env["brew.recipe"].sudo().create(
            {"name": str(payload.get("name") or "").strip() or product.name, "code": code, "company_id": self.company.id, "product_tmpl_id": product.id, "equipment_id": equipment.id, "beer_style": payload.get("beer_style"), "beer_category_id": category.id, "beer_subcategory_id": subcategory.id, "description": payload.get("description")}
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

    def delete_recipe(self, recipe_id, payload):
        self.ensure_manage()
        recipe = self._recipe(recipe_id)
        self._check_version(recipe, payload.get("version"))
        if any(version.state != "draft" for version in recipe.version_ids):
            raise ApiError("VALIDATION_STATE", "只有所有版本均为草稿的配方工艺可以删除。", 409)
        if self.env["brew.production.plan"].sudo().search_count([("recipe_version_id", "in", recipe.version_ids.ids)]):
            raise ApiError("VALIDATION_STATE", "已关联生产计划的配方工艺不能删除。", 409)
        recipe.unlink()

    def list_versions(self, recipe_id):
        recipe = self._recipe(recipe_id)
        return [self.serialize_version(item) for item in recipe.version_ids.sorted(lambda item: (-item.version, item.id))]

    def list_production_runs(self, recipe_id, page=1, page_size=20):
        recipe = self._recipe(recipe_id)
        domain = [
            ("company_id", "=", self.company.id),
            ("recipe_version_id.recipe_id", "=", recipe.id),
        ]
        domain.append(("factory_id", "in", self.user.brew_allowed_factories(self.company).ids))
        model = self.env["brew.production.batch"].sudo()
        records = model.search(domain, order="create_date desc,id desc")
        rows = []
        batched_plan_ids = set(records.mapped("production_plan_id").ids)
        for batch in records:
            base_version = batch.recipe_version_id
            snapshot = json.loads(batch.recipe_snapshot_json or "{}")
            rows.append({
                "id": str(batch.id),
                "version_type": "production",
                "version_label": f"生产批次版 · {batch.name}",
                "name": batch.name,
                "state": batch.state,
                "plan": {"id": str(batch.production_plan_id.id), "name": batch.production_plan_id.name},
                "product": {"id": str(batch.product_tmpl_id.id), "name": batch.product_tmpl_id.display_name},
                "factory": {"id": str(batch.factory_id.id), "name": batch.factory_id.name},
                "equipment": {"id": str(batch.production_plan_id.equipment_id.id), "name": batch.production_plan_id.equipment_id.display_name},
                "tank": {"id": str(batch.tank_id.id), "name": batch.tank_id.display_name},
                "planned_volume_l": batch.planned_volume_l,
                "actual_volume_l": batch.actual_volume_l,
                "planned_start": batch.production_plan_id.planned_start.isoformat() if batch.production_plan_id.planned_start else None,
                "started_at": batch.started_at.isoformat() if batch.started_at else None,
                "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
                "base_version": {
                    "id": str(base_version.id),
                    "number": base_version.version,
                    "label": f"基础工艺 V{base_version.version:02d}",
                },
                "recipe_snapshot": snapshot,
                "material_snapshot": json.loads(batch.material_snapshot_json or "{}"),
                "_sort_at": batch.create_date.isoformat() if batch.create_date else "",
            })
        plan_domain = [
            ("company_id", "=", self.company.id),
            ("recipe_version_id.recipe_id", "=", recipe.id),
            ("production_design_json", "not in", [False, "", "{}"]),
        ]
        plan_domain.append(("factory_id", "in", self.user.brew_allowed_factories(self.company).ids))
        plans = self.env["brew.production.plan"].sudo().search(plan_domain, order="create_date desc,id desc")
        for plan in plans:
            if plan.id in batched_plan_ids:
                continue
            base_version = plan.recipe_version_id
            rows.append({
                "id": f"plan-{plan.id}", "version_type": "production",
                "version_label": f"生产计划版 · {plan.name}", "name": "待生成生产批次",
                "state": "draft", "plan": {"id": str(plan.id), "name": plan.name},
                "product": {"id": str(plan.product_tmpl_id.id), "name": plan.product_tmpl_id.display_name},
                "factory": {"id": str(plan.factory_id.id), "name": plan.factory_id.name},
                "equipment": {"id": str(plan.equipment_id.id), "name": plan.equipment_id.display_name},
                "tank": {"id": str(plan.tank_id.id), "name": plan.tank_id.display_name},
                "planned_volume_l": plan.planned_volume_l, "actual_volume_l": 0,
                "planned_start": plan.planned_start.isoformat() if plan.planned_start else None,
                "started_at": None, "completed_at": None,
                "base_version": {"id": str(base_version.id), "number": base_version.version, "label": f"基础工艺 V{base_version.version:02d}"},
                "recipe_snapshot": json.loads(plan.recipe_snapshot_json or "{}"),
                "material_snapshot": json.loads(plan.material_snapshot_json or "{}"),
                "_sort_at": plan.create_date.isoformat() if plan.create_date else "",
            })
        rows.sort(key=lambda item: item.get("_sort_at", ""), reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        rows = rows[start:start + page_size]
        for row in rows:
            row.pop("_sort_at", None)
        return rows, total

    def create_version(self, recipe_id, payload):
        self.ensure_manage()
        recipe = self._recipe(recipe_id)
        design = payload.get("design") or {}
        if int(design.get("brew_equipment_id") or 0) != recipe.equipment_id.id:
            raise ApiError("VALIDATION_RELATION", "配方版本必须使用该啤酒配方关联的酿造系统。", 422)
        next_version = max(recipe.version_ids.mapped("version") or [0]) + 1
        detail_values = design_values(self.env, self.company, design, payload.get("base_volume_l") or 1000, self.user, recipe.product_tmpl_id.id)
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
            design = payload.get("design") or {}
            if int(design.get("brew_equipment_id") or 0) != version.recipe_id.equipment_id.id:
                raise ApiError("VALIDATION_RELATION", "配方版本必须使用该啤酒配方关联的酿造系统。", 422)
            values["design_json"] = json.dumps(design, ensure_ascii=False, sort_keys=True)
            values.update(design_values(self.env, self.company, design, payload.get("base_volume_l", version.base_volume_l), self.user, version.recipe_id.product_tmpl_id.id))
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
        if action == "submit":
            self.ensure_manage()
        else:
            self.ensure_review()
        version = self._version(version_id)
        self._check_version(version, payload.get("record_version"))
        if action == "submit":
            version.action_submit(self.user)
            self._complete_recipe_revision_tasks(version)
            self._create_recipe_review_tasks(version)
        elif action == "approve":
            comment = payload.get("comment")
            version.action_approve(self.user, comment=comment)
            self._complete_recipe_review_tasks(version, f"审核通过：{comment}" if comment else "审核通过")
        elif action == "return":
            reason = payload.get("reason")
            version.action_return(reason, user=self.user)
            self._complete_recipe_review_tasks(version, f"退回修改：{reason}")
            self._create_recipe_revision_task(version)
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
