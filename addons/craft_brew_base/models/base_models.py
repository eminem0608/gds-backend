from odoo import fields, models


class BrewFactory(models.Model):
    _name = "brew.factory"
    _description = "Brewery Factory"
    _order = "name"
    _check_company_auto = True

    name = fields.Char("厂区名称", required=True)
    code = fields.Char("厂区编码", required=True)
    company_id = fields.Many2one(
        "res.company", string="企业", required=True, index=True, default=lambda self: self.env.company
    )
    address = fields.Char("地址")
    manager_id = fields.Many2one("res.users", string="负责人", check_company=True)
    active = fields.Boolean("启用", default=True)
    stock_warehouse_id = fields.Many2one(
        "stock.warehouse", string="标准仓库", check_company=True, copy=False
    )

    _sql_constraints = [
        ("company_code_unique", "unique(company_id, code)", "同一企业内厂区编码必须唯一。"),
    ]


class BrewWarehouse(models.Model):
    _name = "brew.warehouse"
    _description = "Brewery Warehouse"
    _order = "factory_id, name"
    _check_company_auto = True

    name = fields.Char("仓库名称", required=True)
    factory_id = fields.Many2one("brew.factory", string="所属厂区", required=True, check_company=True)
    company_id = fields.Many2one(
        "res.company", string="企业", related="factory_id.company_id", store=True, index=True
    )
    warehouse_type = fields.Selection(
        [
            ("raw_material", "原料仓"),
            ("packaging", "包装物仓"),
            ("semi_finished", "半成品库"),
            ("finished", "成品库"),
            ("qc", "待检库"),
            ("loss", "损耗库"),
        ],
        string="仓库类型",
        required=True,
        default="raw_material",
    )
    active = fields.Boolean("启用", default=True)
    stock_location_id = fields.Many2one(
        "stock.location", string="标准库位", check_company=True, copy=False
    )

    _sql_constraints = [
        ("factory_name_unique", "unique(factory_id, name)", "同一厂区内仓库名称必须唯一。"),
    ]


class BrewEquipment(models.Model):
    _name = "brew.equipment"
    _description = "Brewery Equipment"
    _order = "factory_id, name"
    _check_company_auto = True

    name = fields.Char("设备名称", required=True)
    factory_id = fields.Many2one("brew.factory", string="所属厂区", required=True, check_company=True)
    company_id = fields.Many2one(
        "res.company", string="企业", related="factory_id.company_id", store=True, index=True
    )
    equipment_type = fields.Selection(
        [
            ("mash", "糖化设备"),
            ("fermentation", "发酵设备"),
            ("bright", "清酒设备"),
            ("packaging", "包装设备"),
            ("centrifuge", "离心机"),
            ("cleaning", "清洗设备"),
            ("other", "其他"),
        ],
        string="设备类型",
        required=True,
    )
    capacity_l = fields.Float("容量(L)")
    status = fields.Selection(
        [
            ("available", "可用"),
            ("busy", "使用中"),
            ("cleaning", "清洗中"),
            ("maintenance", "维护中"),
            ("disabled", "停用"),
        ],
        string="状态",
        default="available",
        required=True,
    )
    active = fields.Boolean("启用", default=True)
    maintenance_equipment_id = fields.Many2one(
        "maintenance.equipment", string="标准设备", check_company=True, copy=False
    )

    _sql_constraints = [
        ("factory_equipment_name_unique", "unique(factory_id, name)", "同一厂区内设备名称必须唯一。"),
    ]


class BrewTank(models.Model):
    _name = "brew.tank"
    _description = "Fermentation or Bright Tank"
    _order = "factory_id, name"
    _check_company_auto = True

    name = fields.Char("罐号", required=True)
    factory_id = fields.Many2one("brew.factory", string="所属厂区", required=True, check_company=True)
    company_id = fields.Many2one(
        "res.company", string="企业", related="factory_id.company_id", store=True, index=True
    )
    equipment_id = fields.Many2one("brew.equipment", string="旧设备引用", check_company=True)
    maintenance_equipment_id = fields.Many2one(
        "maintenance.equipment", string="所属设备", check_company=True
    )
    stock_location_id = fields.Many2one(
        "stock.location", string="库存位置", check_company=True, ondelete="restrict"
    )
    tank_type = fields.Selection(
        [("fermentation", "发酵罐"), ("bright", "清酒罐")],
        string="罐体类型",
        default="fermentation",
        required=True,
    )
    capacity_l = fields.Float("容量(L)", required=True)
    current_product = fields.Char("当前产品")
    current_batch_no = fields.Char("当前批次")
    current_volume_l = fields.Float("当前体积(L)")
    current_temperature = fields.Float("当前温度(℃)")
    current_pressure = fields.Float("当前压力(bar)")
    current_gravity = fields.Float("当前糖度")
    status = fields.Selection(
        [
            ("empty", "空罐"),
            ("fermenting", "发酵中"),
            ("conditioning", "后发酵"),
            ("cold_crash", "冷贮"),
            ("ready", "待包装"),
            ("cleaning", "清洗中"),
            ("maintenance", "维护中"),
            ("abnormal", "异常"),
        ],
        string="状态",
        default="empty",
        required=True,
    )
    cleaning_status = fields.Selection(
        [("clean", "已清洗"), ("dirty", "待清洗"), ("unknown", "未知")],
        string="清洗状态",
        default="unknown",
        required=True,
    )
    active = fields.Boolean("启用", default=True)

    _sql_constraints = [
        ("factory_tank_name_unique", "unique(factory_id, name)", "同一厂区内罐号必须唯一。"),
        ("tank_capacity_positive", "check(capacity_l > 0)", "罐体容量必须大于零。"),
    ]


class BrewSupplier(models.Model):
    _name = "brew.supplier"
    _description = "Brewery Supplier"
    _order = "name"
    _check_company_auto = True

    name = fields.Char("供应商名称", required=True)
    company_id = fields.Many2one(
        "res.company", string="企业", required=True, index=True, default=lambda self: self.env.company
    )
    contact_name = fields.Char("联系人")
    phone = fields.Char("电话")
    address = fields.Char("地址")
    bank_account = fields.Char("银行信息")
    supply_category = fields.Char("供货品类")
    active = fields.Boolean("启用", default=True)
    partner_id = fields.Many2one("res.partner", string="标准供应商", check_company=True, copy=False)

    _sql_constraints = [
        ("company_supplier_name_unique", "unique(company_id, name)", "同一企业内供应商名称必须唯一。"),
    ]


class BrewCustomer(models.Model):
    _name = "brew.customer"
    _description = "Brewery Customer"
    _order = "name"
    _check_company_auto = True

    name = fields.Char("客户名称", required=True)
    company_id = fields.Many2one(
        "res.company", string="企业", required=True, index=True, default=lambda self: self.env.company
    )
    contact_name = fields.Char("联系人")
    phone = fields.Char("电话")
    delivery_address = fields.Char("收货地址")
    customer_level = fields.Selection(
        [("normal", "普通"), ("key", "重点"), ("vip", "VIP")],
        string="客户等级",
        default="normal",
    )
    active = fields.Boolean("启用", default=True)
    partner_id = fields.Many2one("res.partner", string="标准客户", check_company=True, copy=False)

    _sql_constraints = [
        ("company_customer_name_unique", "unique(company_id, name)", "同一企业内客户名称必须唯一。"),
    ]


class BrewMaterial(models.Model):
    _name = "brew.material"
    _description = "Brewery Material"
    _order = "category, name"
    _check_company_auto = True

    name = fields.Char("原料名称", required=True)
    company_id = fields.Many2one(
        "res.company", string="企业", required=True, index=True, default=lambda self: self.env.company
    )
    category = fields.Selection(
        [
            ("malt", "麦芽"),
            ("hop", "酒花"),
            ("yeast", "酵母"),
            ("adjunct", "辅料"),
            ("packaging", "包装物"),
        ],
        string="分类",
        required=True,
    )
    brand = fields.Char("品牌")
    supplier_id = fields.Many2one("brew.supplier", string="供应商", check_company=True)
    specification = fields.Char("规格")
    uom = fields.Selection(
        [("kg", "kg"), ("g", "g"), ("l", "L"), ("unit", "个"), ("box", "箱")],
        string="单位",
        required=True,
        default="kg",
    )
    price = fields.Float("单价")
    shelf_life_days = fields.Integer("保质期(天)")
    extract_rate = fields.Float("浸出率/发酵度(%)")
    color_ebc = fields.Float("色度(EBC)")
    alpha_acid = fields.Float("Alpha酸(%)")
    flavor_desc = fields.Text("风味描述")
    active = fields.Boolean("启用", default=True)
    product_tmpl_id = fields.Many2one(
        "product.template", string="标准产品", check_company=True, copy=False
    )

    _sql_constraints = [
        (
            "company_material_name_spec_unique",
            "unique(company_id, name, specification)",
            "同一企业内物料名称和规格组合必须唯一。",
        ),
        ("shelf_life_non_negative", "check(shelf_life_days >= 0)", "保质期不能为负数。"),
    ]
