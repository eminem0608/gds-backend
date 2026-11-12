"""Seed an idempotent pilot dataset through ``odoo shell``.

This file is intentionally not loaded by the module. Run it explicitly against the
development database so production databases can never receive sample records during
module installation or upgrade.
"""

import json
from datetime import timedelta

from odoo import fields


ALLOWED_DATABASES = {"craft_brew_dev"}

if env.cr.dbname not in ALLOWED_DATABASES:
    raise RuntimeError(f"Refusing to seed non-development database: {env.cr.dbname}")


def get_or_create(model_name, domain, values):
    model = env[model_name].sudo().with_context(active_test=False)
    record = model.search(domain, limit=1)
    return record or model.create(values)


company = env.company
admin = env.ref("base.user_admin").sudo().with_company(company)
factory = env["brew.factory"].sudo().search(
    [("company_id", "=", company.id), ("code", "=", "PILOT")], limit=1
)
if not factory:
    factory = env["brew.factory"].sudo().create(
        {
            "name": "试运行厂区",
            "code": "PILOT",
            "company_id": company.id,
            "address": "试运行生产地址",
            "manager_id": admin.id,
        }
    )
if not factory.stock_warehouse_id:
    warehouse = env["stock.warehouse"].sudo().create(
        {
            "name": factory.name,
            "code": "PILOT",
            "company_id": company.id,
            "brew_factory_id": factory.id,
        }
    )
    factory.stock_warehouse_id = warehouse
warehouse = factory.stock_warehouse_id
admin.write(
    {
        "brew_factory_ids": [(4, factory.id)],
        "brew_default_factory_id": factory.id,
    }
)


def ensure_location(name, location_type):
    location = get_or_create(
        "stock.location",
        [
            ("company_id", "=", company.id),
            ("location_id", "=", warehouse.view_location_id.id),
            ("name", "=", name),
        ],
        {
            "name": name,
            "location_id": warehouse.view_location_id.id,
            "usage": "internal",
            "company_id": company.id,
            "brew_factory_id": factory.id,
            "brew_location_type": location_type,
        },
    )
    location.write(
        {"brew_factory_id": factory.id, "brew_location_type": location_type, "active": True}
    )
    return location


locations = {
    "raw": ensure_location("原料库", "raw_material"),
    "packaging": ensure_location("包装物库", "packaging"),
    "qc": ensure_location("待检库", "qc"),
    "semi": ensure_location("生产暂存库", "semi_finished"),
    "finished": ensure_location("成品库", "finished"),
    "loss": ensure_location("损耗库", "loss"),
}


def ensure_equipment(name, equipment_type, capacity_l, status="available"):
    equipment = get_or_create(
        "maintenance.equipment",
        [
            ("company_id", "=", company.id),
            ("brew_factory_id", "=", factory.id),
            ("name", "=", name),
        ],
        {
            "name": name,
            "company_id": company.id,
            "brew_factory_id": factory.id,
            "brew_equipment_type": equipment_type,
            "brew_capacity_l": capacity_l,
            "brew_status": status,
        },
    )
    equipment.write(
        {
            "brew_equipment_type": equipment_type,
            "brew_capacity_l": capacity_l,
            "brew_status": status,
            "active": True,
        }
    )
    return equipment


mash_equipment = ensure_equipment("PILOT-MASH-01 糖化系统", "mash", 2000)
fermentation_equipment = ensure_equipment("PILOT-FERM-01 发酵系统", "fermentation", 4000, "busy")
bright_equipment = ensure_equipment("PILOT-BBT-01 清酒系统", "bright", 2000, "busy")
ensure_equipment("PILOT-PACK-01 包装线", "packaging", 1200)


def ensure_tank(name, tank_type, capacity_l, equipment, **status_values):
    tank = get_or_create(
        "brew.tank",
        [("factory_id", "=", factory.id), ("name", "=", name)],
        {
            "name": name,
            "factory_id": factory.id,
            "tank_type": tank_type,
            "capacity_l": capacity_l,
            "maintenance_equipment_id": equipment.id,
            **status_values,
        },
    )
    tank.write(
        {
            "tank_type": tank_type,
            "capacity_l": capacity_l,
            "maintenance_equipment_id": equipment.id,
            "active": True,
            **status_values,
        }
    )
    return tank


empty_tank = ensure_tank(
    "F-01", "fermentation", 2000, fermentation_equipment,
    status="empty", cleaning_status="clean", current_product=False,
    current_batch_no=False, current_volume_l=0,
)
ensure_tank(
    "F-02", "fermentation", 2000, fermentation_equipment,
    status="fermenting", cleaning_status="clean", current_product="试运行 IPA",
    current_batch_no="PILOT-BATCH-001", current_volume_l=1680,
    current_temperature=19.2, current_pressure=0.8, current_gravity=6.8,
)
ensure_tank(
    "BBT-01", "bright", 2000, bright_equipment,
    status="ready", cleaning_status="clean", current_product="试运行 IPA",
    current_batch_no="PILOT-BATCH-000", current_volume_l=920,
    current_temperature=2.1, current_pressure=1.2, current_gravity=2.8,
)

kg = env.ref("uom.product_uom_kgm")
unit = env.ref("uom.product_uom_unit")


def ensure_product(code, name, item_type, uom, price, category=False, **extra):
    template = get_or_create(
        "product.template",
        [("company_id", "=", company.id), ("default_code", "=", code)],
        {
            "name": name,
            "default_code": code,
            "company_id": company.id,
            "detailed_type": "product",
            "purchase_ok": item_type != "finished_product",
            "sale_ok": item_type == "finished_product",
            "tracking": "lot",
            "uom_id": uom.id,
            "uom_po_id": uom.id,
            "standard_price": price,
            "brew_item_type": item_type,
            "brew_material_category": category,
            **extra,
        },
    )
    return template


malt = ensure_product(
    "RM-MALT-PALE", "淡色基础麦芽", "raw_material", kg, 8.5, "malt",
    brew_brand="Pilot Malt", brew_specification="25kg/袋", brew_shelf_life_days=365,
    brew_extract_rate=80.0, brew_color_ebc=4.0,
)
hops = ensure_product(
    "RM-HOP-CITRA", "Citra 酒花", "raw_material", kg, 168.0, "hop",
    brew_brand="Pilot Hop", brew_specification="5kg/袋", brew_shelf_life_days=730,
    brew_alpha_acid=13.5, brew_flavor_desc="柑橘与热带水果",
)
yeast = ensure_product(
    "RM-YEAST-ALE", "艾尔酵母", "raw_material", kg, 95.0, "yeast",
    brew_specification="500g/包", brew_shelf_life_days=180,
)
carton = ensure_product(
    "PK-BOX-24", "24 罐装纸箱", "packaging_material", unit, 4.2, "packaging",
    brew_specification="24罐/箱", brew_shelf_life_days=0,
)
finished = ensure_product(
    "FG-PILOT-IPA", "试运行 IPA", "finished_product", unit, 0.0,
    brew_brand="试运行系列", brew_specification="20L桶 / 24罐箱",
)

packaging = get_or_create(
    "product.packaging",
    [("product_id", "=", finished.product_variant_id.id), ("name", "=", "20L 桶")],
    {
        "name": "20L 桶",
        "product_id": finished.product_variant_id.id,
        "company_id": company.id,
        "qty": 1,
        "brew_barcode_level": "keg",
        "brew_volume_l": 20,
        "brew_tare_weight_kg": 8.5,
    },
)
get_or_create(
    "product.packaging",
    [("product_id", "=", finished.product_variant_id.id), ("name", "=", "24 罐箱")],
    {
        "name": "24 罐箱",
        "product_id": finished.product_variant_id.id,
        "company_id": company.id,
        "qty": 24,
        "brew_barcode_level": "box",
        "brew_volume_l": 7.92,
    },
)

supplier = get_or_create(
    "res.partner",
    [("company_id", "=", company.id), ("name", "=", "试运行原料供应商")],
    {
        "name": "试运行原料供应商",
        "company_id": company.id,
        "supplier_rank": 1,
        "phone": "13800000001",
        "brew_supply_category": "麦芽、酒花、酵母",
    },
)
customer = get_or_create(
    "res.partner",
    [("company_id", "=", company.id), ("name", "=", "试运行渠道客户")],
    {
        "name": "试运行渠道客户",
        "company_id": company.id,
        "customer_rank": 1,
        "phone": "13800000002",
        "brew_customer_level": "key",
    },
)


def ensure_lot_stock(template, lot_name, supplier_lot, expiration_days, quantity, location):
    product = template.product_variant_id
    lot = get_or_create(
        "stock.lot",
        [
            ("company_id", "=", company.id),
            ("product_id", "=", product.id),
            ("name", "=", lot_name),
        ],
        {
            "name": lot_name,
            "product_id": product.id,
            "company_id": company.id,
            "brew_supplier_lot": supplier_lot,
            "brew_expiration_date": fields.Date.today() + timedelta(days=expiration_days),
            "brew_quality_status": "qualified",
            "brew_source_model": "seed.dev.data",
        },
    )
    lot.write(
        {
            "brew_supplier_lot": supplier_lot,
            "brew_expiration_date": fields.Date.today() + timedelta(days=expiration_days),
            "brew_quality_status": "qualified",
        }
    )
    quant = env["stock.quant"].sudo().search(
        [
            ("company_id", "=", company.id),
            ("product_id", "=", product.id),
            ("location_id", "=", location.id),
            ("lot_id", "=", lot.id),
        ],
        limit=1,
    )
    difference = quantity - (quant.quantity if quant else 0)
    if difference:
        env["stock.quant"].sudo()._update_available_quantity(
            product, location, difference, lot_id=lot
        )
    return lot


ensure_lot_stock(malt, "LOT-MALT-202609", "SUP-MALT-0901", 120, 680, locations["raw"])
ensure_lot_stock(hops, "LOT-HOP-202609", "SUP-HOP-0905", 24, 26, locations["raw"])
ensure_lot_stock(yeast, "LOT-YEAST-202609", "SUP-YEAST-0908", 12, 8, locations["raw"])
ensure_lot_stock(carton, "LOT-BOX-202609", "SUP-BOX-0901", 365, 500, locations["packaging"])

recipe = get_or_create(
    "brew.recipe",
    [("company_id", "=", company.id), ("code", "=", "PILOT-IPA")],
    {
        "name": "试运行 IPA 标准配方",
        "code": "PILOT-IPA",
        "company_id": company.id,
        "product_tmpl_id": finished.id,
        "beer_style": "American IPA",
        "description": "试运行厂区首批验证配方",
    },
)
version = env["brew.recipe.version"].sudo().search(
    [("recipe_id", "=", recipe.id), ("version", "=", 1)], limit=1
)
if not version:
    version = env["brew.recipe.version"].sudo().create(
        {
            "recipe_id": recipe.id,
            "version": 1,
            "base_volume_l": 1000,
            "original_gravity": 14.5,
            "final_gravity": 2.8,
            "bitterness_ibu": 45,
            "color_ebc": 12,
            "alcohol_abv": 6.2,
            "notes": "成本为原料标准价估算，不作为财务成本。",
            "material_line_ids": [
                (0, 0, {"sequence": 10, "product_id": malt.product_variant_id.id, "quantity": 220, "uom_id": kg.id, "stage": "mash"}),
                (0, 0, {"sequence": 20, "product_id": hops.product_variant_id.id, "quantity": 8, "uom_id": kg.id, "stage": "boil"}),
                (0, 0, {"sequence": 30, "product_id": yeast.product_variant_id.id, "quantity": 2, "uom_id": kg.id, "stage": "fermentation"}),
            ],
            "process_step_ids": [
                (0, 0, {"sequence": 10, "name": "糖化记录", "phase": "mash", "action_type": "record", "target_duration_hours": 1.5, "target_temperature": 67, "config_json": "{}"}),
                (0, 0, {"sequence": 20, "name": "进罐确认", "phase": "fermentation", "action_type": "enter_tank", "mandatory_confirmation": True, "config_json": "{}"}),
                (0, 0, {"sequence": 30, "name": "发酵记录", "phase": "fermentation", "action_type": "record", "target_duration_hours": 168, "target_temperature": 19, "config_json": "{}"}),
                (0, 0, {"sequence": 40, "name": "转罐确认", "phase": "conditioning", "action_type": "transfer", "mandatory_confirmation": True, "config_json": "{}"}),
                (0, 0, {"sequence": 50, "name": "包装放行", "phase": "packaging", "action_type": "packaging_release", "mandatory_confirmation": True, "config_json": "{}"}),
            ],
        }
    )
    version.action_submit(admin)
    version.action_approve(admin)

demand = get_or_create(
    "brew.product.demand",
    [("company_id", "=", company.id), ("name", "=", "DEMO-DEMAND-001")],
    {
        "name": "DEMO-DEMAND-001",
        "company_id": company.id,
        "factory_id": factory.id,
        "salesperson_id": admin.id,
        "customer_id": customer.id,
        "product_tmpl_id": finished.id,
        "packaging_id": packaging.id,
        "package_qty": 50,
        "requested_volume_l": 1000,
        "requested_delivery_date": fields.Date.today() + timedelta(days=14),
        "customer_reference": "PILOT-PO-001",
        "notes": "试运行订单，用于验证需求到排产流程。",
    },
)
if demand.state == "draft":
    demand.action_submit()
if demand.state == "submitted":
    demand.action_confirm(admin)

plan = get_or_create(
    "brew.production.plan",
    [("company_id", "=", company.id), ("name", "=", "DEMO-PLAN-001")],
    {
        "name": "DEMO-PLAN-001",
        "company_id": company.id,
        "factory_id": factory.id,
        "demand_id": demand.id,
        "product_tmpl_id": finished.id,
        "recipe_version_id": version.id,
        "planned_volume_l": 1000,
        "planned_start": fields.Datetime.now() + timedelta(days=1),
        "planned_end": fields.Datetime.now() + timedelta(days=15),
        "equipment_id": mash_equipment.id,
        "tank_id": empty_tank.id,
        "owner_id": admin.id,
    },
)
if plan.state == "draft":
    plan.check_resources()
if plan.material_snapshot_json in {False, "[]", "{}"}:
    plan.expand_materials()

task_specs = [
    ("DEMO-TASK-001", "核对明日糖化准备", "production_preparation", "operator", "1"),
    ("DEMO-TASK-002", "复核临期酵母批次", "inventory_expiry", "warehouse_keeper", "2"),
    ("DEMO-TASK-003", "确认试运行客户交期", "demand_followup", "salesperson", "0"),
]
for code, name, task_type, role_code, priority in task_specs:
    get_or_create(
        "brew.task",
        [("company_id", "=", company.id), ("target_model", "=", "seed.dev.data"), ("target_id", "=", int(code.rsplit("-", 1)[1]))],
        {
            "name": name,
            "task_type": task_type,
            "company_id": company.id,
            "factory_id": factory.id,
            "role_code": role_code,
            "target_model": "seed.dev.data",
            "target_id": int(code.rsplit("-", 1)[1]),
            "priority": priority,
            "due_at": fields.Datetime.now() + timedelta(days=2),
        },
    )

rules = [
    ("alert", "inventory-expiry", "库存效期预警", {"warning_days": 30, "critical_days": 7}),
    ("approval", "material-overage", "超领审批阈值", {"enabled": True, "overage_rate": 0.05, "steps": [{"role_code": "production_director"}]}),
    ("barcode", "pilot-v1", "试运行条码规则", {"levels": ["lot", "keg", "box"], "reserve_levels": ["bottle", "can"]}),
]
for kind, code, name, config in rules:
    get_or_create(
        "brew.configuration.rule",
        [("company_id", "=", company.id), ("factory_id", "=", factory.id), ("rule_kind", "=", kind), ("code", "=", code), ("version", "=", 1)],
        {
            "name": name,
            "code": code,
            "rule_kind": kind,
            "company_id": company.id,
            "factory_id": factory.id,
            "version": 1,
            "state": "active",
            "effective_at": fields.Datetime.now(),
            "config_json": json.dumps(config, ensure_ascii=False, sort_keys=True),
        },
    )

env.cr.commit()
print(
    json.dumps(
        {
            "database": env.cr.dbname,
            "factory": factory.code,
            "locations": len(locations),
            "equipment": env["maintenance.equipment"].sudo().search_count([("brew_factory_id", "=", factory.id)]),
            "tanks": env["brew.tank"].sudo().search_count([("factory_id", "=", factory.id)]),
            "products": env["product.template"].sudo().search_count([("company_id", "=", company.id), ("default_code", "like", "PILOT")]),
            "recipe": recipe.code,
            "demand": demand.name,
            "plan": plan.name,
            "tasks": env["brew.task"].sudo().search_count([("target_model", "=", "seed.dev.data")]),
        },
        ensure_ascii=False,
    )
)
