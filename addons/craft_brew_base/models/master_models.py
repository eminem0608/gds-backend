import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError


ITEM_TYPES = [
    ("raw_material", "原料"),
    ("packaging_material", "包装物"),
    ("semi_finished", "半成品"),
    ("finished_product", "成品"),
]

MATERIAL_CATEGORIES = [
    ("malt", "麦芽"),
    ("hop", "酒花"),
    ("yeast", "酵母"),
    ("adjunct", "辅料"),
    ("packaging", "包装物"),
]

LOCATION_TYPES = [
    ("raw_material", "原料"),
    ("packaging", "包装物"),
    ("qc", "待检"),
    ("semi_finished", "半成品"),
    ("finished", "成品"),
    ("loss", "损耗"),
]

EQUIPMENT_TYPES = [
    ("mash", "糖化设备"),
    ("fermentation", "发酵设备"),
    ("bright", "清酒设备"),
    ("packaging", "包装设备"),
    ("centrifuge", "离心机"),
    ("cleaning", "清洗设备"),
    ("other", "其他"),
]


class ProductTemplate(models.Model):
    _inherit = "product.template"
    _check_company_auto = True

    brew_item_type = fields.Selection(ITEM_TYPES, string="精酿物料类型", index=True)
    brew_material_category = fields.Selection(MATERIAL_CATEGORIES, string="原料分类", index=True)
    brew_brand = fields.Char("品牌")
    brew_specification = fields.Char("规格")
    brew_shelf_life_days = fields.Integer("保质期(天)", default=0)
    brew_extract_rate = fields.Float("浸出率/发酵度(%)")
    brew_color_ebc = fields.Float("色度(EBC)")
    brew_alpha_acid = fields.Float("Alpha酸(%)")
    brew_flavor_desc = fields.Text("风味描述")
    brew_legacy_material_id = fields.Many2one("brew.material", copy=False, index=True)

    _sql_constraints = [
        (
            "brew_shelf_life_non_negative",
            "check(brew_shelf_life_days >= 0)",
            "保质期不能为负数。",
        ),
    ]

    @api.constrains("brew_item_type", "brew_material_category")
    def _check_brew_material_category(self):
        for product in self:
            if product.brew_item_type == "raw_material" and not product.brew_material_category:
                raise ValidationError("原料必须选择原料分类。")


class ProductPackaging(models.Model):
    _inherit = "product.packaging"

    active = fields.Boolean(default=True)
    brew_barcode_level = fields.Selection(
        [("keg", "桶"), ("box", "箱"), ("bottle", "瓶"), ("can", "罐")],
        string="条码层级",
    )
    brew_volume_l = fields.Float("单包装容量(L)")
    brew_tare_weight_kg = fields.Float("皮重(kg)")


class ResPartner(models.Model):
    _inherit = "res.partner"

    brew_customer_level = fields.Selection(
        [("normal", "普通"), ("key", "重点"), ("vip", "VIP")],
        string="客户等级",
        default="normal",
    )
    brew_supply_category = fields.Char("供货品类")
    brew_legacy_supplier_id = fields.Many2one("brew.supplier", copy=False, index=True)
    brew_legacy_customer_id = fields.Many2one("brew.customer", copy=False, index=True)


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"
    _check_company_auto = True

    brew_factory_id = fields.Many2one(
        "brew.factory", string="所属厂区", check_company=True, index=True
    )

    _sql_constraints = [
        ("brew_factory_unique", "unique(brew_factory_id)", "一个厂区只能关联一个标准仓库。"),
    ]

    @api.model_create_multi
    def create(self, values_list):
        warehouses = super().create(values_list)
        warehouses._sync_brew_factory_locations()
        return warehouses

    def write(self, values):
        result = super().write(values)
        if "brew_factory_id" in values:
            self._sync_brew_factory_locations()
        return result

    def _sync_brew_factory_locations(self):
        location_model = self.env["stock.location"].sudo()
        for warehouse in self.filtered("brew_factory_id"):
            locations = location_model.search(
                [("id", "child_of", warehouse.view_location_id.id)]
            )
            locations.write({"brew_factory_id": warehouse.brew_factory_id.id})


class StockLocation(models.Model):
    _inherit = "stock.location"
    _check_company_auto = True

    brew_factory_id = fields.Many2one(
        "brew.factory", string="所属厂区", check_company=True, index=True
    )
    brew_location_type = fields.Selection(LOCATION_TYPES, string="精酿库位类型", index=True)
    brew_legacy_warehouse_id = fields.Many2one("brew.warehouse", copy=False, index=True)


class MaintenanceEquipment(models.Model):
    _inherit = "maintenance.equipment"
    _check_company_auto = True

    brew_factory_id = fields.Many2one(
        "brew.factory", string="所属厂区", check_company=True, index=True
    )
    brew_equipment_type = fields.Selection(EQUIPMENT_TYPES, string="设备类型", index=True)
    brew_capacity_l = fields.Float("容量(L)")
    brew_status = fields.Selection(
        [
            ("available", "可用"),
            ("busy", "使用中"),
            ("cleaning", "清洗中"),
            ("maintenance", "维护中"),
            ("disabled", "停用"),
        ],
        string="生产状态",
        default="available",
        required=True,
        index=True,
    )
    brew_legacy_equipment_id = fields.Many2one("brew.equipment", copy=False, index=True)


class BrewEquipmentUnavailability(models.Model):
    _name = "brew.equipment.unavailability"
    _description = "Craft Brew Equipment Unavailability"
    _order = "start_at, id"
    _check_company_auto = True

    name = fields.Char(required=True)
    equipment_id = fields.Many2one(
        "maintenance.equipment", required=True, check_company=True, ondelete="cascade", index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="equipment_id.brew_factory_id", store=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", related="equipment_id.company_id", store=True, index=True
    )
    start_at = fields.Datetime(required=True, index=True)
    end_at = fields.Datetime(required=True, index=True)
    reason = fields.Text()
    state = fields.Selection(
        [("active", "生效"), ("cancelled", "已取消")],
        required=True,
        default="active",
        index=True,
    )

    @api.constrains("start_at", "end_at", "equipment_id", "state")
    def _check_interval(self):
        for period in self:
            if period.start_at >= period.end_at:
                raise ValidationError("不可用结束时间必须晚于开始时间。")
            if period.state != "active":
                continue
            overlap = self.search_count(
                [
                    ("id", "!=", period.id),
                    ("equipment_id", "=", period.equipment_id.id),
                    ("state", "=", "active"),
                    ("start_at", "<", period.end_at),
                    ("end_at", ">", period.start_at),
                ]
            )
            if overlap:
                raise ValidationError("同一设备的不可用时段不能重叠。")

    def action_cancel(self):
        self.write({"state": "cancelled"})
        return True


class BrewConfigurationRule(models.Model):
    _name = "brew.configuration.rule"
    _description = "Versioned Craft Brew Configuration Rule"
    _order = "rule_kind, code, version desc"
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    rule_kind = fields.Selection(
        [
            ("number", "编号规则"),
            ("barcode", "条码规则"),
            ("alert", "预警规则"),
            ("approval", "审批规则"),
        ],
        required=True,
        index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    version = fields.Integer(required=True, default=1)
    state = fields.Selection(
        [("draft", "草稿"), ("active", "生效"), ("inactive", "停用")],
        required=True,
        default="draft",
        index=True,
    )
    effective_at = fields.Datetime()
    config_json = fields.Text(required=True, default="{}")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "scope_kind_code_version_unique",
            "unique(company_id, factory_id, rule_kind, code, version)",
            "同一范围内规则编码和版本必须唯一。",
        ),
        ("version_positive", "check(version > 0)", "规则版本必须大于零。"),
    ]

    @api.constrains("company_id", "factory_id", "rule_kind", "code", "version")
    def _check_rule_version_unique(self):
        for rule in self:
            count = self.search_count(
                [
                    ("id", "!=", rule.id),
                    ("company_id", "=", rule.company_id.id),
                    ("factory_id", "=", rule.factory_id.id or False),
                    ("rule_kind", "=", rule.rule_kind),
                    ("code", "=", rule.code),
                    ("version", "=", rule.version),
                ]
            )
            if count:
                raise ValidationError("同一范围内规则编码和版本必须唯一。")

    @api.constrains("config_json")
    def _check_config_json(self):
        for rule in self:
            try:
                value = json.loads(rule.config_json or "{}")
            except (TypeError, ValueError) as exc:
                raise ValidationError("规则配置必须是有效的 JSON。") from exc
            if not isinstance(value, dict):
                raise ValidationError("规则配置必须是 JSON 对象。")

    def action_activate(self):
        for rule in self:
            if rule.state != "draft":
                raise ValidationError("只有草稿规则可以生效。")
            domain = [
                ("id", "!=", rule.id),
                ("company_id", "=", rule.company_id.id),
                ("factory_id", "=", rule.factory_id.id or False),
                ("rule_kind", "=", rule.rule_kind),
                ("code", "=", rule.code),
                ("state", "=", "active"),
            ]
            self.search(domain).write({"state": "inactive"})
            rule.write({"state": "active", "effective_at": fields.Datetime.now()})
        return True

    def action_deactivate(self):
        invalid = self.filtered(lambda rule: rule.state != "active")
        if invalid:
            raise ValidationError("只有生效规则可以停用。")
        self.write({"state": "inactive"})
        return True
