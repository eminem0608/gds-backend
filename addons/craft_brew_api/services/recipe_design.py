"""Validate the editor's v2 document and build canonical recipe lines atomically."""
import json
import math

from odoo import Command
from ..controllers.base import ApiError


def invalid(message):
    raise ApiError("VALIDATION_FORMAT", message, 422)


def number(value, label, minimum=None, maximum=None, optional=False):
    if optional and value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        invalid(f"{label}必须是数值。")
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        invalid(f"{label}超出有效范围。")
    return result


def mash_water_requirements(mash, volume, grain_weight):
    """Original document formulas; existing snapshots keep their hourly model."""
    original = mash.get("water_calculation") == "original"
    boil_factor = 1 if original else float(mash.get("boil_time_min") or 0) / 60
    pre_volume = volume * (1 + float(mash.get("evaporation_rate") or 0) / 100 * boil_factor)
    mash_water = grain_weight * float(mash.get("water_grain_ratio") or 0)
    first = mash_water if original else max(0, mash_water - grain_weight * float(mash.get("absorption_rate") or 0) - float(mash.get("screen_water_l") or 0))
    return pre_volume, max(0, pre_volume - first)


def design_values(env, company, design, volume, user=None, product_id=None):
    if not isinstance(design, dict):
        invalid("配方设计必须为对象。")
    if design.get("schema_version") != 2:
        return {}
    volume = number(volume, "酿造体积", 0.1)
    if number(design.get("brew_volume_l"), "设计体积", 0.1) != volume:
        invalid("设计体积与版本体积不一致。")
    equipment_id = number(design.get("brew_equipment_id"), "酿造设备", 1)
    equipment = env["maintenance.equipment"].sudo().search([
        ("id", "=", int(equipment_id)), ("company_id", "=", company.id), ("brew_equipment_type", "=", "mash")], limit=1)
    if not equipment or (equipment.brew_capacity_l and volume > equipment.brew_capacity_l):
        invalid("请选择当前企业的酿造设备，体积不得超过设备容量。")
    mash = design.get("mash") or {}
    fermentation = design.get("fermentation") or {}
    for key, low, high in [("efficiency", .1, 100), ("water_grain_ratio", .1, None), ("absorption_rate", 0, None), ("boil_time_min", 1, None), ("evaporation_rate", 0, 100), ("screen_water_l", 0, None)]:
        number(mash.get(key), key, low, high)
    count = number(mash.get("sparge_count"), "洗糟次数", 1, 10)
    if count != int(count):
        invalid("洗糟次数必须是整数。")
    method = design.get("ibu_method")
    if method not in {"tinseth", "fixed"}:
        invalid("请选择有效的苦味值计算模式。")
    number(design.get("attenuation_override"), "配方发酵度", 0, 100, optional=True)
    for key, value in (design.get("targets") or {}).items():
        number(value, f"目标 {key}", .9 if key == "fg" else 1 if key == "og" else 0, optional=True)
    material_commands = [Command.clear()]
    grain_weight = 0
    for key, allowed, unit, stage in [("fermentables", {"malt", "adjunct"}, "uom.product_uom_kgm", "mash"), ("hops", {"hop"}, "uom.product_uom_gram", "boil"), ("yeast", {"yeast"}, "uom.product_uom_gram", "fermentation")]:
        rows = design.get(key, [])
        if not isinstance(rows, list):
            invalid("原料明细必须为列表。")
        for row in rows:
            product_id = number(row.get("material_id"), "供应商商品", 1)
            template = env["product.template"].sudo().search([("id", "=", int(product_id)), ("company_id", "=", company.id), ("brew_item_type", "=", "raw_material")], limit=1)
            category = env["brew.material.category"].sudo().search([("code", "=", template.brew_material_category)], limit=1) if template else False
            group = category.material_group if category else (template.brew_material_category if template else None)
            if not template or not template.brew_supplier_id or group not in allowed:
                invalid("原料必须是当前企业对应种类的供应商商品。")
            quantity = number(row.get("quantity"), "原料数量", .001)
            uom = env.ref(unit)
            if template.uom_id.category_id != uom.category_id:
                invalid(f"{template.name} 的计量单位不是重量单位，请先在物料中修正。")
            line_stage = stage
            if key == "fermentables":
                grain_weight += quantity
                number(row.get("extract_rate"), "浸出率", 0, 100, optional=True)
                number(row.get("color"), "麦芽色度", 0, optional=True)
            elif key == "hops":
                number(row.get("alpha_acid"), "Alpha酸", 0, 100, optional=True)
                use = row.get("use")
                if use not in {"boil", "FWH", "WP", "DH", "0"}:
                    invalid("酒花投放阶段无效。")
                if use == "boil":
                    minutes = number(row.get("time"), "酒花煮沸时长", 0, float(mash["boil_time_min"]))
                    if method == "fixed" and minutes not in {0, 30, 60}:
                        invalid("固定异构率支持 0、30、60 分钟。")
                if method == "tinseth" and use in {"FWH", "WP"}:
                    number(row.get("utilization_override"), "工艺利用率", 0, 100)
                line_stage = "fermentation" if use == "DH" else "mash" if use == "FWH" else "boil"
            else:
                number(row.get("attenuation"), "酵母发酵度", 0, 100, optional=True)
                number(row.get("temperature"), "酵母发酵温度", optional=True)
            material_commands.append(Command.create({"sequence": len(material_commands) * 10, "product_id": template.product_variant_id.id, "quantity": quantity, "uom_id": uom.id, "stage": line_stage, "notes": row.get("notes") or (str(row.get("use", "")) if key == "hops" else "")}))
    waters = mash.get("sparge_water_liters", [])
    if len(waters) != int(count):
        invalid("洗糟次数与各次用水量不一致。")
    total = sum(number(w, "洗糟水量", 0) for w in waters)
    _pre_volume, sparge_water = mash_water_requirements(mash, volume, grain_weight)
    if abs(total - sparge_water) > (.005 if mash.get("water_calculation") == "original" else .05):
        invalid("洗糟水量合计与所需水量不一致。")
    water = design.get('water_adjustment')
    if water and water.get('enabled'):
        from .water import WaterService
        from odoo.addons.craft_brew_workflow.models.water_chemistry import validate_adjustment
        water_service=WaterService(env,user or env.user,company)
        water_service.validate_recipe(water,equipment)
        water_service.validate_target(water,equipment,product_id)
        try: validate_adjustment(water,design)
        except (ValueError,TypeError) as exc: invalid(str(exc))
    steps = [Command.clear()]

    def step(name, phase, action="record", config=None, temperature=0, duration=0):
        steps.append(Command.create({"sequence": len(steps) * 10, "name": name, "phase": phase, "action_type": action, "mandatory_confirmation": action in {"enter_tank", "transfer", "packaging_release"}, "target_temperature": temperature or 0, "target_duration_hours": duration or 0, "config_json": json.dumps(config or {}, ensure_ascii=False, sort_keys=True)}))

    for row in mash.get("steps", []):
        if not str(row.get("name") or "").strip():
            invalid("糖化步骤名称不能为空。")
        step(row["name"], "mash", config=row, temperature=number(row.get("temperature"), "糖化温度", 0, 100), duration=number(row.get("duration_min"), "糖化时间", 1) / 60)
    workflow = mash.get("workflow") or {}
    if workflow.get("execution_mode") == "manual":
        operations = (workflow.get("operations") or {}).get("manual", [])
        if not isinstance(operations, list):
            invalid("工艺步骤必须为列表。")
        operation_ids = set()
        for row in operations:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                invalid("工艺步骤名称不能为空。")
            code = row.get("id")
            if not isinstance(code, str) or not code.strip() or code.startswith("rest_") or code in operation_ids:
                invalid("工艺步骤标识无效或重复。")
            operation_ids.add(code)
            mode = row.get("input_mode", "confirm")
            if mode not in {"confirm", "choice", "number", "text"}:
                invalid("工艺步骤记录方式无效。")
            if mode == "choice" and not any(item.strip() for item in str(row.get("target") or "").split("/")):
                invalid("请填写工艺步骤的可选结果，并用 / 分隔。")
            step(row["name"], "mash", config=row)
    else:
        step("煮沸", "mash", config={"boil_time_min": mash["boil_time_min"]}, duration=float(mash["boil_time_min"]) / 60)
    step("进罐确认", "fermentation", "enter_tank", temperature=number(fermentation.get("target_temperature"), "目标发酵温度", optional=True))
    actions = {"measure_gravity": "测量糖度", "measure_temperature": "测量温度", "measure_pressure": "测量压力", "measure_diacetyl": "测量双乙酰", "seal": "封罐", "dry_hop": "干投", "cool_to": "降温", "dump_hops": "排出酒花", "centrifuge": "离心", "increase_pressure": "升压", "package": "出酒"}
    for row in fermentation.get("steps", []):
        condition = row.get("condition")
        action = row.get("action")
        if condition not in {"time", "gravity", "attenuation", "diacetyl", "temperature", "pressure"} or action not in actions:
            invalid("发酵条件或处理动作无效。")
        if condition == "time":
            schedule = row.get("schedule_type")
            if schedule not in {"daily", "day", "interval"}:
                invalid("请选择每天、第几天或每隔几天。")
            if schedule != "daily":
                days = number(row.get("day_value" if schedule == "day" else "interval_days"), "发酵条件天数", 1)
                if days != int(days):
                    invalid("发酵条件天数必须为整数。")
        else:
            if row.get("operator") not in {"=", ">", ">=", "<", "<="}:
                invalid("条件比较符无效。")
            number(row.get("condition_value"), "条件达成值")
        if action in {"cool_to", "increase_pressure"}:
            number(row.get("target"), "处理目标值")
        mapped = {"cool_to": "cool_down", "dry_hop": "dry_hop", "centrifuge": "centrifuge"}.get(action, "record")
        step(actions[action], "fermentation", mapped, config={"fermentation_rule": row}, temperature=float(row["target"]) if action == "cool_to" else 0)
    number(fermentation.get("target_days"), "目标发酵天数", 0, optional=True)
    cold = number(fermentation.get("cold_storage_temperature"), "冷贮温度", optional=True)
    if cold is not None:
        step("冷贮", "conditioning", "cold_store", temperature=cold)
    step("转罐确认", "conditioning", "transfer")
    step("包装放行确认", "packaging", "packaging_release")
    return {"material_line_ids": material_commands, "process_step_ids": steps}
