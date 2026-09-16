import json

from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


REQUIRED_CONFIRMATION_ACTIONS = {"enter_tank", "transfer", "packaging_release"}


class BrewRecipe(models.Model):
    _name = "brew.recipe"
    _description = "Craft Beer Recipe"
    _order = "code, name"
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    product_tmpl_id = fields.Many2one(
        "product.template",
        required=True,
        check_company=True,
        domain="[('brew_item_type', '=', 'finished_product')]",
    )
    beer_style = fields.Char()
    description = fields.Text()
    version_ids = fields.One2many("brew.recipe.version", "recipe_id")
    active_version_id = fields.Many2one(
        "brew.recipe.version", compute="_compute_active_version", store=True
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("company_code_unique", "unique(company_id, code)", "同一企业内配方编码必须唯一。"),
        (
            "company_product_unique",
            "unique(company_id, product_tmpl_id)",
            "同一企业内一个成品只能关联一个配方。",
        ),
    ]

    @api.depends("version_ids.state")
    def _compute_active_version(self):
        for recipe in self:
            recipe.active_version_id = recipe.version_ids.filtered(
                lambda item: item.state == "active"
            )[:1]


class BrewRecipeVersion(models.Model):
    _name = "brew.recipe.version"
    _description = "Craft Beer Recipe Version"
    _order = "recipe_id, version desc"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True)
    recipe_id = fields.Many2one(
        "brew.recipe", required=True, ondelete="cascade", check_company=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="recipe_id.company_id", store=True, index=True
    )
    version = fields.Integer(required=True, default=1)
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("submitted", "待审核"),
            ("approved", "已审核"),
            ("active", "使用中"),
            ("inactive", "已停用"),
            ("returned", "已退回"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    base_volume_l = fields.Float(required=True, default=1000.0)
    original_gravity = fields.Float()
    final_gravity = fields.Float()
    bitterness_ibu = fields.Float()
    color_ebc = fields.Float()
    alcohol_abv = fields.Float()
    estimated_cost = fields.Monetary(currency_field="currency_id", readonly=True)
    currency_id = fields.Many2one(
        "res.currency", related="company_id.currency_id", store=True
    )
    material_line_ids = fields.One2many("brew.recipe.material", "version_id", copy=True)
    process_step_ids = fields.One2many("brew.recipe.process.step", "version_id", copy=True)
    submitted_by_id = fields.Many2one("res.users", check_company=True)
    submitted_at = fields.Datetime()
    approved_by_id = fields.Many2one("res.users", check_company=True)
    approved_at = fields.Datetime()
    return_reason = fields.Text()
    notes = fields.Text()

    _sql_constraints = [
        ("recipe_version_unique", "unique(recipe_id, version)", "配方版本号必须唯一。"),
        ("version_positive", "check(version > 0)", "版本号必须大于零。"),
        ("base_volume_positive", "check(base_volume_l > 0)", "基准体积必须大于零。"),
    ]

    _protected_fields = {
        "recipe_id",
        "version",
        "base_volume_l",
        "original_gravity",
        "final_gravity",
        "bitterness_ibu",
        "color_ebc",
        "alcohol_abv",
        "notes",
    }

    def write(self, values):
        if not self.env.context.get("brew_state_transition") and self._protected_fields.intersection(values):
            self._ensure_draft()
        return super().write(values)

    @api.depends("recipe_id.code", "version")
    def _compute_name(self):
        for version in self:
            version.name = f"{version.recipe_id.code or '-'}-V{version.version}"

    def _ensure_draft(self):
        if any(item.state not in {"draft", "returned"} for item in self):
            raise ValidationError("只有草稿或已退回版本可以修改。")

    def action_submit(self, user=None):
        self.ensure_one()
        self._ensure_draft()
        if not self.material_line_ids:
            raise ValidationError("配方至少需要一条原料。")
        if not self.process_step_ids:
            raise ValidationError("配方至少需要一个工艺节点。")
        confirmations = set(
            self.process_step_ids.filtered("mandatory_confirmation").mapped("action_type")
        )
        missing = sorted(REQUIRED_CONFIRMATION_ACTIONS - confirmations)
        if missing:
            raise ValidationError(f"缺少强制确认节点：{', '.join(missing)}")
        estimate = self.calculate(self.base_volume_l)["estimated_cost"]
        self.with_context(brew_state_transition=True).write(
            {
                "state": "submitted",
                "estimated_cost": estimate,
                "submitted_by_id": (user or self.env.user).id,
                "submitted_at": fields.Datetime.now(),
                "return_reason": False,
            }
        )
        return True

    def action_approve(self, user=None):
        self.ensure_one()
        if self.state != "submitted":
            raise ValidationError("只有待审核版本可以审核通过。")
        self.with_context(brew_state_transition=True).write(
            {
                "state": "approved",
                "approved_by_id": (user or self.env.user).id,
                "approved_at": fields.Datetime.now(),
            }
        )
        return True

    def action_return(self, reason):
        self.ensure_one()
        if self.state != "submitted" or not str(reason or "").strip():
            raise ValidationError("只有待审核版本可以退回，且必须填写原因。")
        self.with_context(brew_state_transition=True).write(
            {"state": "returned", "return_reason": reason}
        )
        return True

    def action_activate(self):
        self.ensure_one()
        if self.state != "approved":
            raise ValidationError("只有已审核版本可以启用。")
        current = self.recipe_id.version_ids.filtered(lambda item: item.state == "active")
        current.with_context(brew_state_transition=True).write({"state": "inactive"})
        self.with_context(brew_state_transition=True).write({"state": "active"})
        return True

    def action_deactivate(self):
        self.ensure_one()
        if self.state != "active":
            raise ValidationError("只有使用中的版本可以停用。")
        self.with_context(brew_state_transition=True).write({"state": "inactive"})
        return True

    def copy_as_draft(self):
        self.ensure_one()
        next_version = max(self.recipe_id.version_ids.mapped("version") or [0]) + 1
        return self.copy(
            {
                "version": next_version,
                "state": "draft",
                "submitted_by_id": False,
                "submitted_at": False,
                "approved_by_id": False,
                "approved_at": False,
                "return_reason": False,
            }
        )

    def calculate(self, volume_l):
        self.ensure_one()
        if volume_l <= 0:
            raise ValidationError("计算体积必须大于零。")
        factor = volume_l / self.base_volume_l
        lines = []
        total = 0.0
        for line in self.material_line_ids.sorted("sequence"):
            quantity = line.quantity * factor
            unit_cost = line.product_id.standard_price
            quantity_in_product_uom = line.uom_id._compute_quantity(
                quantity, line.product_id.uom_id
            )
            cost = quantity_in_product_uom * unit_cost
            total += cost
            lines.append(
                {
                    "material_id": str(line.product_id.id),
                    "material_name": line.product_id.display_name,
                    "quantity": quantity,
                    "uom_id": str(line.uom_id.id),
                    "uom_name": line.uom_id.name,
                    "estimated_unit_cost": unit_cost,
                    "estimated_cost": cost,
                }
            )
        return {"volume_l": volume_l, "materials": lines, "estimated_cost": total, "currency": self.currency_id.name}


class BrewRecipeMaterial(models.Model):
    _name = "brew.recipe.material"
    _description = "Craft Beer Recipe Material"
    _order = "sequence, id"
    _check_company_auto = True

    version_id = fields.Many2one(
        "brew.recipe.version", required=True, ondelete="cascade", check_company=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="version_id.company_id", store=True, index=True
    )
    sequence = fields.Integer(default=10)
    product_id = fields.Many2one(
        "product.product",
        required=True,
        check_company=True,
        domain="[('product_tmpl_id.brew_item_type', 'in', ['raw_material', 'packaging_material'])]",
    )
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    stage = fields.Selection(
        [("mash", "糖化"), ("boil", "煮沸"), ("fermentation", "发酵"), ("packaging", "包装")],
        required=True,
        default="mash",
    )
    notes = fields.Char()

    _sql_constraints = [
        ("quantity_positive", "check(quantity > 0)", "原料数量必须大于零。"),
    ]

    @api.constrains("uom_id", "product_id")
    def _check_uom_category(self):
        for line in self:
            if line.uom_id.category_id != line.product_id.uom_id.category_id:
                raise ValidationError("配方原料单位必须与物料单位属于同一类别。")

    @api.model_create_multi
    def create(self, values_list):
        versions = self.env["brew.recipe.version"].browse(
            [values.get("version_id") for values in values_list if values.get("version_id")]
        )
        versions._ensure_draft()
        return super().create(values_list)

    def write(self, values):
        self.mapped("version_id")._ensure_draft()
        return super().write(values)

    def unlink(self):
        self.mapped("version_id")._ensure_draft()
        return super().unlink()


class BrewRecipeProcessStep(models.Model):
    _name = "brew.recipe.process.step"
    _description = "Craft Beer Recipe Process Step"
    _order = "sequence, id"
    _check_company_auto = True

    version_id = fields.Many2one(
        "brew.recipe.version", required=True, ondelete="cascade", check_company=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="version_id.company_id", store=True, index=True
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    phase = fields.Selection(
        [("mash", "糖化"), ("fermentation", "发酵"), ("conditioning", "后处理"), ("packaging", "包装")],
        required=True,
    )
    action_type = fields.Selection(
        [
            ("record", "记录"),
            ("enter_tank", "进罐"),
            ("dry_hop", "干投"),
            ("discharge_yeast", "排酵母"),
            ("cool_down", "降温"),
            ("cold_store", "冷贮"),
            ("add_adjunct", "加辅料"),
            ("add_fruit", "加水果"),
            ("filter", "过滤"),
            ("centrifuge", "离心"),
            ("pasteurize", "杀菌"),
            ("taste", "品鉴"),
            ("transfer", "转罐"),
            ("packaging_release", "包装放行"),
        ],
        required=True,
        default="record",
    )
    mandatory_confirmation = fields.Boolean(default=False)
    target_duration_hours = fields.Float()
    target_temperature = fields.Float()
    config_json = fields.Text(default="{}")

    @api.constrains("action_type", "mandatory_confirmation", "config_json")
    def _check_step(self):
        for step in self:
            if step.action_type in REQUIRED_CONFIRMATION_ACTIONS and not step.mandatory_confirmation:
                raise ValidationError("进罐、转罐和包装放行必须强制确认。")
            try:
                config = json.loads(step.config_json or "{}")
            except (TypeError, ValueError) as exc:
                raise ValidationError("工艺节点配置必须是有效 JSON。") from exc
            if not isinstance(config, dict):
                raise ValidationError("工艺节点配置必须是 JSON 对象。")

    @api.model_create_multi
    def create(self, values_list):
        versions = self.env["brew.recipe.version"].browse(
            [values.get("version_id") for values in values_list if values.get("version_id")]
        )
        versions._ensure_draft()
        return super().create(values_list)

    def write(self, values):
        self.mapped("version_id")._ensure_draft()
        return super().write(values)

    def unlink(self):
        self.mapped("version_id")._ensure_draft()
        return super().unlink()
