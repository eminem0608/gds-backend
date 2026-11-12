import json
import math

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
    ("material_issue", "领料"),
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
    brew_material_category = fields.Selection(
        selection="_brew_material_category_selection", string="原料分类", index=True
    )
    brew_brand = fields.Char("品牌")
    brew_specification = fields.Char("规格")
    brew_beer_category_id = fields.Many2one(
        "brew.beer.category", string="啤酒大类", ondelete="restrict", index=True
    )
    brew_beer_subcategory_id = fields.Many2one(
        "brew.beer.category", string="啤酒小类", ondelete="restrict", index=True
    )
    brew_shelf_life_days = fields.Integer("保质期(天)", default=0)
    brew_extract_rate = fields.Float("浸出率/发酵度(%)")
    brew_color_ebc = fields.Float("色度(EBC)")
    brew_supplier_price = fields.Float("供应商商品价格")
    brew_supplier_quantity = fields.Float("供应商商品数量")
    brew_alpha_acid = fields.Float("Alpha酸(%)")
    brew_flavor_desc = fields.Text("风味描述")
    brew_beta_acid = fields.Float("β酸(%)")
    brew_cohumulone_pct = fields.Float("合葎草酮(%)")
    brew_total_oil_ml_100g = fields.Float("总油(mL/100g)")
    brew_attenuation_min = fields.Float("最低发酵度(%)")
    brew_attenuation_max = fields.Float("最高发酵度(%)")
    brew_fermentation_temp_min = fields.Float("最低发酵温度(℃)")
    brew_fermentation_temp_max = fields.Float("最高发酵温度(℃)")
    brew_flocculation = fields.Char("絮凝程度")
    brew_alcohol_tolerance_pct = fields.Float("耐醇度(%)")
    brew_product_model = fields.Char("英文名称/型号")
    brew_hop_type = fields.Char("酒花用途")
    brew_yeast_style = fields.Char("酵母风格")
    # 麦芽/可发酵物供应商检测与批次信息（均为选填）
    brew_moisture_pct = fields.Float("水分(%)")
    brew_total_protein_pct = fields.Float("总蛋白(%)")
    brew_extract_dry_pct = fields.Float("浸出物(干基)(%)")
    brew_diastatic_power_wk = fields.Float("糖化力(WK)")
    brew_friability_pct = fields.Float("脆度(%)")
    brew_kolbach_index_pct = fields.Float("库尔巴哈值(%)")
    brew_beta_dextran_mg_l = fields.Float("β-葡聚糖(mg/L)")
    brew_fan_mg_100g = fields.Float("α-氨基氮(mg/100g)")
    brew_total_nitrogen_pct = fields.Float("总氮(%)")
    brew_soluble_nitrogen_pct = fields.Float("可溶性氮(%)")
    brew_filtration_time_min = fields.Float("过滤时间(min/300ml)")
    brew_saccharification_time_min = fields.Float("糖化时间(min)")
    brew_impurity_pct = fields.Float("夹杂物(%)")
    brew_fc_difference_pct = fields.Float("粗细粉差(F/C)(%)")
    brew_germination_rate_pct = fields.Float("发芽率(%)")
    brew_total_acidity = fields.Float("总酸(ml/100ml)")
    brew_ph = fields.Float("pH")
    brew_boiled_color_ebc = fields.Float("煮沸色度(EBC)")
    brew_turbidity_ebc = fields.Float("浊度(EBC)")
    brew_ota_ug_kg = fields.Float("赭曲霉毒素OTA(μg/kg)")
    brew_fusarium_ug_kg = fields.Float("脱氧雪腐镰刀菌烯醇(μg/kg)")
    brew_batch_no = fields.Char("批次号")
    brew_production_date = fields.Date("生产日期")
    brew_release_date = fields.Date("出厂日期")
    brew_malt_class = fields.Char("麦芽级别")
    brew_barley_variety = fields.Char("大麦品种")
    brew_sample_source = fields.Char("样品来源")
    brew_sensory_desc = fields.Text("口感品评")
    brew_supplier_id = fields.Many2one("res.partner", string="供应商", index=True, check_company=True)
    brew_legacy_material_id = fields.Many2one("brew.material", copy=False, index=True)

    @api.model
    def _brew_material_category_selection(self):
        defaults = [(code, label) for code, label in MATERIAL_CATEGORIES]
        categories = self.env["brew.material.category"].sudo().search(
            [("active", "=", True)], order="material_group, sequence, id"
        )
        values = dict(defaults)
        values.update({item.code: item.name for item in categories})
        return list(values.items())

    _sql_constraints = [
        (
            "brew_shelf_life_non_negative",
            "check(brew_shelf_life_days >= 0)",
            "保质期不能为负数。",
        ),
    ]

    @api.constrains("brew_beta_acid", "brew_cohumulone_pct", "brew_total_oil_ml_100g", "brew_attenuation_min", "brew_attenuation_max", "brew_fermentation_temp_min", "brew_fermentation_temp_max", "brew_alcohol_tolerance_pct")
    def _check_brew_product_parameters(self):
        percentages = ["brew_beta_acid", "brew_cohumulone_pct", "brew_attenuation_min", "brew_attenuation_max", "brew_alcohol_tolerance_pct"]
        for product in self:
            for name in percentages + ["brew_total_oil_ml_100g", "brew_fermentation_temp_min", "brew_fermentation_temp_max"]:
                value = product[name]
                if not math.isfinite(value) or (name in percentages and not 0 <= value <= 100) or (name == "brew_total_oil_ml_100g" and value < 0):
                    raise ValidationError("原料参数格式或范围不正确，百分比须在 0–100 之间。")
            for low, high in [("brew_attenuation_min", "brew_attenuation_max"), ("brew_fermentation_temp_min", "brew_fermentation_temp_max")]:
                if product[low] and product[high] and product[low] > product[high]:
                    raise ValidationError("原料参数下限不能大于上限。")

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
    brew_supplier_code = fields.Char("供应商编码", copy=False, index=True)
    brew_contact_name = fields.Char("联系人")
    brew_social_credit_code = fields.Char("统一社会信用代码")
    brew_bank_account_name = fields.Char("账户名")
    brew_bank_name = fields.Char("银行名称")
    brew_bank_branch = fields.Char("分行名称")
    brew_bank_account = fields.Char("银行账号")
    brew_legacy_supplier_id = fields.Many2one("brew.supplier", copy=False, index=True)
    brew_legacy_customer_id = fields.Many2one("brew.customer", copy=False, index=True)


class BrewMaterialCategory(models.Model):
    _name = "brew.material.category"
    _description = "System Material Category"
    _order = "material_group, sequence, id"

    name = fields.Char("名称", required=True)
    code = fields.Char("编码", required=True, index=True)
    material_group = fields.Char("原料大类标识", required=True, index=True)
    parent_id = fields.Many2one("brew.material.category", string="上级种类", ondelete="restrict")
    sequence = fields.Integer("排序", default=10)
    active = fields.Boolean("启用", default=True)

    _sql_constraints = [
        ("material_category_code_unique", "unique(code)", "原料种类编码必须唯一。"),
    ]


class BrewBeerCategory(models.Model):
    _name = "brew.beer.category"
    _description = "System Beer Category"
    _order = "parent_id, sequence, id"

    name = fields.Char("名称", required=True)
    code = fields.Char("编码", required=True, index=True)
    parent_id = fields.Many2one("brew.beer.category", string="上级分类", ondelete="restrict")
    sequence = fields.Integer("排序", default=10)
    active = fields.Boolean("启用", default=True)

    _sql_constraints = [
        ("beer_category_code_unique", "unique(code)", "啤酒分类编码必须唯一。"),
    ]


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"
    _check_company_auto = True

    brew_warehouse_type = fields.Selection(
        [
            ("raw_material", "原料仓"),
            ("material_issue", "领料库"),
            ("packaging", "包装物仓"),
            ("semi_finished", "半成品仓"),
            ("finished", "成品仓"),
            ("qc", "待检仓"),
            ("loss", "损耗仓"),
            ("tank", "罐体仓"),
        ], string="仓库类型", default="raw_material", required=True, index=True
    )

    brew_factory_id = fields.Many2one(
        "brew.factory", string="所属厂区", check_company=True, index=True
    )

    @api.model_create_multi
    def create(self, values_list):
        warehouses = super().create(values_list)
        warehouses._sync_brew_factory_locations()
        return warehouses

    def write(self, values):
        result = super().write(values)
        if "brew_factory_id" in values or "brew_warehouse_type" in values:
            self._sync_brew_factory_locations()
        return result

    def _sync_brew_factory_locations(self):
        location_model = self.env["stock.location"].sudo()
        for warehouse in self.filtered("brew_factory_id"):
            locations = location_model.search(
                [("id", "child_of", warehouse.view_location_id.id)]
            )
            locations.write({"brew_factory_id": warehouse.brew_factory_id.id})
            warehouse.lot_stock_id.write({"brew_location_type": "semi_finished" if warehouse.brew_warehouse_type == "tank" else warehouse.brew_warehouse_type})
            if warehouse.brew_warehouse_type == "material_issue" and not location_model.search_count([("location_id", "=", warehouse.lot_stock_id.id), ("usage", "=", "internal")]):
                location_model.create({"name": "领料暂存区", "location_id": warehouse.lot_stock_id.id,
                    "usage": "internal", "company_id": warehouse.company_id.id,
                    "brew_factory_id": warehouse.brew_factory_id.id, "brew_location_type": "material_issue"})


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
    brew_tank_volume_l = fields.Float("罐体积(L)")
    brew_tank_count = fields.Integer("罐体数量")
    brew_equipment_type_custom = fields.Char("自定义设备类型")
    brew_saccharification_efficiency = fields.Float("糖化效率(%)")
    brew_screen_water_l = fields.Float("筛板余水(L)")
    brew_whirlpool_time_min = fields.Float("回旋时间(分钟)")
    brew_filtration_speed_l_min = fields.Float("过滤速度(L/min)")
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
