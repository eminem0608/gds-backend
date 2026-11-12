import base64
import binascii
import math
from io import BytesIO

from PIL import Image, UnidentifiedImageError
from odoo import fields

from ..controllers.base import ApiError


RESOURCE_CONFIG = {
    "factories": {
        "model": "brew.factory",
        "write_groups": ["craft_brew_core.group_brew_admin"],
        "fields": {"name", "code", "address", "manager_id"},
    },
    "equipment": {
        "model": "maintenance.equipment",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
        ],
        "fields": {
            "name", "brew_factory_id", "brew_equipment_type", "brew_equipment_type_custom",
            "brew_capacity_l", "brew_tank_volume_l", "brew_tank_count", "brew_status", "brew_saccharification_efficiency",
            "brew_screen_water_l", "brew_whirlpool_time_min", "brew_filtration_speed_l_min",
        },
    },
    "tanks": {
        "model": "brew.tank",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
        ],
        "fields": {"name", "factory_id", "maintenance_equipment_id", "stock_location_id", "tank_type", "capacity_l", "cleaning_status"},
    },
    "warehouses": {
        "model": "stock.warehouse",
        "write_groups": ["craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_brewery_manager"],
        "fields": {"name", "code", "brew_factory_id", "brew_warehouse_type", "lot_stock_id"},
    },
    "locations": {
        "model": "stock.location",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
                "craft_brew_core.group_brew_brewery_manager",
        ],
        "fields": {"name", "brew_factory_id", "brew_location_type", "location_id", "usage"},
    },
    "products": {
        "model": "product.template",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
        ],
        "fields": {"name", "default_code", "brew_item_type", "brew_brand", "brew_specification", "brew_beer_category_id", "brew_beer_subcategory_id", "uom_id", "uom_po_id", "tracking", "standard_price"},
    },
    "materials": {
        "model": "product.template",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
            "craft_brew_core.group_brew_purchase",
            "craft_brew_core.group_brew_supplier",
        ],
        "fields": {"name", "default_code", "brew_item_type", "brew_material_category", "brew_brand", "brew_specification", "image_1920", "uom_id", "uom_po_id", "tracking", "standard_price", "brew_shelf_life_days", "brew_extract_rate", "brew_color_ebc", "brew_supplier_price", "brew_supplier_quantity", "brew_alpha_acid", "brew_flavor_desc", "brew_supplier_id", "brew_moisture_pct", "brew_total_protein_pct", "brew_extract_dry_pct", "brew_diastatic_power_wk", "brew_friability_pct", "brew_kolbach_index_pct", "brew_beta_dextran_mg_l", "brew_fan_mg_100g", "brew_total_nitrogen_pct", "brew_soluble_nitrogen_pct", "brew_filtration_time_min", "brew_saccharification_time_min", "brew_impurity_pct", "brew_fc_difference_pct", "brew_germination_rate_pct", "brew_total_acidity", "brew_ph", "brew_boiled_color_ebc", "brew_turbidity_ebc", "brew_ota_ug_kg", "brew_fusarium_ug_kg", "brew_batch_no", "brew_production_date", "brew_release_date", "brew_malt_class", "brew_barley_variety", "brew_sample_source", "brew_sensory_desc", "brew_beta_acid", "brew_cohumulone_pct", "brew_total_oil_ml_100g", "brew_attenuation_min", "brew_attenuation_max", "brew_fermentation_temp_min", "brew_fermentation_temp_max", "brew_flocculation", "brew_alcohol_tolerance_pct", "brew_product_model", "brew_hop_type", "brew_yeast_style"},
    },
    "material-categories": {
        "model": "brew.material.category",
        "write_groups": ["craft_brew_core.group_brew_admin"],
        "fields": {"name", "code", "material_group", "parent_id", "sequence", "brew_packaging_type"},
    },
    "beer-categories": {
        "model": "brew.beer.category",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_general_manager",
        ],
        "fields": {"name", "code", "parent_id", "sequence"},
    },
    "packaging-specs": {
        "model": "product.packaging",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
        ],
        "fields": {"name", "product_id", "qty", "barcode", "brew_barcode_level", "brew_volume_l", "brew_tare_weight_kg"},
    },
    "suppliers": {
        "model": "res.partner",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_purchase",
        ],
        "fields": {"name", "phone", "email", "street", "street2", "city", "comment", "active", "brew_supply_category", "brew_supplier_code", "brew_contact_name", "brew_social_credit_code", "brew_bank_account_name", "brew_bank_name", "brew_bank_branch", "brew_bank_account", "supplier_account_login", "supplier_account_password"},
    },
    "customers": {
        "model": "res.partner",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_sales",
        ],
        "fields": {"name", "phone", "email", "street", "street2", "city", "brew_customer_level", "user_id"},
    },
}


class MasterDataService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company

    def _config(self, resource):
        config = RESOURCE_CONFIG.get(resource)
        if not config:
            raise ApiError("RESOURCE_NOT_FOUND", "主数据资源不存在。", 404)
        return config

    def ensure_write_access(self, resource, create=False):
        config = self._config(resource)
        if create and resource == "products" and self.user.has_group("craft_brew_core.group_brew_brewer"):
            return
        if not any(self.user.has_group(group) for group in config["write_groups"]):
            raise ApiError("PERMISSION_DENIED", "当前账号无权维护该主数据。", 403)

    def _allowed_factory_ids(self):
        return self.user.brew_allowed_factories(self.company).ids

    def _domain(self, resource):
        company_domain = [("company_id", "=", self.company.id)]
        domains = {
            "factories": company_domain,
            "equipment": company_domain + [("brew_factory_id", "!=", False)],
            "tanks": company_domain,
            "warehouses": company_domain + [("brew_factory_id", "!=", False)],
            "locations": company_domain + [("brew_factory_id", "!=", False)],
            "products": company_domain + [("brew_item_type", "in", ["semi_finished", "finished_product"])],
        "materials": company_domain + [("brew_item_type", "in", ["raw_material", "packaging_material"])],
            "packaging-specs": [("product_id.product_tmpl_id.company_id", "=", self.company.id)],
            "suppliers": company_domain + [("supplier_rank", ">", 0)],
            "customers": company_domain + [("customer_rank", ">", 0)],
            "material-categories": [("active", "=", True)],
            "beer-categories": [("active", "=", True)],
        }
        domain = domains[resource]
        if resource == "materials" and not self.user.has_group("craft_brew_core.group_brew_supplier"):
            domain += ["|", ("brew_item_type", "!=", "packaging_material"), "|", ("brew_packaging_state", "=", False), ("brew_packaging_state", "=", "confirmed")]
            domain += ["|", ("brew_item_type", "!=", "packaging_material"), ("brew_packaging_catalog_owner_id", "=", False)]
        if resource == "materials" and self.user.has_group("craft_brew_core.group_brew_supplier"):
            domain += [("brew_supplier_id", "=", self.user.brew_supplier_id.id)]
            domain += ["|", ("brew_item_type", "!=", "packaging_material"), ("brew_packaging_catalog_owner_id", "=", self.user.brew_supplier_id.id)]
        if resource in {"factories", "equipment", "tanks", "warehouses", "locations"}:
            field_name = {
                "factories": "id",
                "equipment": "brew_factory_id",
                "tanks": "factory_id",
                "warehouses": "brew_factory_id",
                "locations": "brew_factory_id",
            }[resource]
            domain += [(field_name, "in", self._allowed_factory_ids())]
        return domain

    def _record(self, resource, record_id, include_archived=True):
        model = self.env[self._config(resource)["model"]].sudo()
        record = model.with_context(active_test=not include_archived).search(
            self._domain(resource) + [("id", "=", int(record_id))], limit=1
        )
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "主数据记录不存在。", 404)
        return record

    def list(self, resource, page, page_size, keyword="", active=None):
        config = self._config(resource)
        model = self.env[config["model"]].sudo().with_context(active_test=False)
        domain = list(self._domain(resource))
        if keyword:
            domain += [("name", "ilike", keyword)]
        # 主数据列表默认只展示有效记录；归档/删除记录仅通过恢复操作进入专门流程。
        if "active" in model._fields and active is not None:
            domain += [("active", "=", active)]
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="name,id",
        )
        if resource == "factories":
            for factory in records:
                self._ensure_tank_warehouse(factory)
        return [self.serialize(resource, item) for item in records], total

    def get(self, resource, record_id):
        return self.serialize(resource, self._record(resource, record_id))

    def _factory(self, value):
        factory = self.env["brew.factory"].sudo().search(
            [("id", "=", int(value)), ("company_id", "=", self.company.id), ("active", "=", True)],
            limit=1,
        )
        if not factory:
            raise ApiError("DATA_SCOPE_DENIED", "厂区不存在、已停用或不属于当前企业。", 403)
        if factory.id not in self._allowed_factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "当前账号只能维护所属厂区的仓库和库位。", 403)
        return factory

    def _values(self, resource, payload, create=False):
        config = self._config(resource)
        special_fields = {"tank_specs"} if resource == "equipment" else set()
        if resource == "materials":
            # Supplier catalog sends the selected commercial weight unit; this
            # is translated to Odoo's uom_id/uom_po_id below, not stored as a
            # product.template field.
            special_fields.add("weight_uom")
        if resource == "suppliers":
            special_fields.update({"supplier_account_login", "supplier_account_password"})
        unknown = set(payload) - config["fields"] - {"version"} - special_fields
        if unknown:
            raise ApiError("VALIDATION_FORMAT", "存在不允许写入的字段。", 422, {"fields": sorted(unknown)})
        values = {key: payload[key] for key in config["fields"] if key in payload}
        if resource == "materials" and values.get("image_1920"):
            encoded = values["image_1920"]
            if not isinstance(encoded, str) or len(encoded) > 7_000_000:
                raise ApiError("VALIDATION_FORMAT", "商品图片不能超过 5 MB。", 422)
            try:
                image_data = base64.b64decode(encoded, validate=True)
                if len(image_data) > 5 * 1024 * 1024:
                    raise ApiError("VALIDATION_FORMAT", "商品图片不能超过 5 MB。", 422)
                with Image.open(BytesIO(image_data)) as image:
                    if image.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ApiError("VALIDATION_FORMAT", "商品图片仅支持 JPG、PNG 或 WebP 格式。", 422)
                    if image.width * image.height > 30_000_000:
                        raise ApiError("VALIDATION_FORMAT", "图片分辨率过大，请压缩后再上传。", 422)
                    image.verify()
            except (binascii.Error, UnidentifiedImageError, OSError, ValueError):
                raise ApiError("VALIDATION_FORMAT", "商品图片文件无效，请重新选择 JPG、PNG 或 WebP 图片。", 422)
        if resource == "suppliers":
            values.pop("supplier_account_login", None)
            values.pop("supplier_account_password", None)
        relation_fields = {
            "manager_id",
            "brew_factory_id",
            "factory_id",
            "maintenance_equipment_id",
            "stock_location_id",
            "location_id",
            "uom_id",
            "uom_po_id",
            "product_id",
            "user_id",
            "parent_id",
            "brew_beer_category_id",
            "brew_beer_subcategory_id",
            "brew_supplier_id",
        }
        for field_name in relation_fields.intersection(values):
            values[field_name] = int(values[field_name]) if values[field_name] else False

        factory_field = {
            "equipment": "brew_factory_id",
            "tanks": "factory_id",
            "warehouses": "brew_factory_id",
            "locations": "brew_factory_id",
        }.get(resource)
        if factory_field in values:
            values[factory_field] = self._factory(values[factory_field]).id
        if create and resource in {"equipment", "tanks", "warehouses", "locations"} and not values.get(factory_field):
            raise ApiError("VALIDATION_REQUIRED", "所属厂区不能为空。", 422)

        if resource in {"products", "materials"}:
            values["company_id"] = self.company.id
            values.setdefault("detailed_type", "product")
            values.setdefault("tracking", "lot")
            if resource == "materials":
                values.setdefault("brew_item_type", "raw_material")
                if values["brew_item_type"] not in {"raw_material", "packaging_material"}:
                    raise ApiError("VALIDATION_FORMAT", "物料类型无效。", 422)
                category_code = values.get("brew_material_category")
                category = self.env["brew.material.category"].sudo().search(
                    [("code", "=", category_code), ("active", "=", True)], limit=1
                ) if category_code else self.env["brew.material.category"]
                if not category or category.material_group == "packaging":
                    raise ApiError("VALIDATION_FORMAT", "供应商商品只能选择系统已有的酿造原料分类。", 422)
                if create or "weight_uom" in payload:
                    unit = payload.get("weight_uom", "kg")
                    if unit not in {"kg", "g"}:
                        raise ApiError("VALIDATION_FORMAT", "酿造原料请选择 kg 或 g 作为计量单位。", 422)
                    uom = self.env.ref("uom.product_uom_kgm" if unit == "kg" else "uom.product_uom_gram")
                    values.update({"uom_id": uom.id, "uom_po_id": uom.id})
                is_supplier = self.user.has_group("craft_brew_core.group_brew_supplier")
                if not is_supplier and values.get("brew_supplier_id"):
                    supplier = self.env["res.partner"].sudo().search([
                        ("id", "=", values["brew_supplier_id"]), ("company_id", "=", self.company.id),
                        ("supplier_rank", ">", 0), ("active", "=", True)], limit=1)
                    if not supplier:
                        raise ApiError("DATA_SCOPE_DENIED", "请选择系统设置中本企业有效的供应商。", 403)
                if is_supplier or values.get("brew_supplier_id"):
                    if not category.parent_id:
                        raise ApiError("VALIDATION_FORMAT", "供应商商品必须选择系统已有的小类。", 422)
                    if is_supplier and (not self.user.brew_supplier_id or self.user.brew_supplier_id.company_id != self.company):
                        raise ApiError("DATA_SCOPE_DENIED", "当前账号未绑定有效供应商。", 403)
                    if is_supplier:
                        values["brew_supplier_id"] = self.user.brew_supplier_id.id
                    required = {
                        "brew_brand": "品牌/生产商",
                        "brew_specification": "规格",
                        "brew_supplier_price": "商品价格",
                        "brew_supplier_quantity": "商品数量",
                    }
                    if category.material_group in {"malt", "adjunct", "yeast"}:
                        required["brew_extract_rate"] = "浸出率/发酵度"
                    if category.material_group in {"malt", "adjunct"}:
                        required["brew_color_ebc"] = "色度"
                    if category.material_group == "hop":
                        required["brew_alpha_acid"] = "Alpha酸"
                    missing = next(
                        (label for key, label in required.items() if values.get(key) is None or (isinstance(values.get(key), str) and not values.get(key).strip())),
                        None,
                    )
                    if missing:
                        raise ApiError("VALIDATION_REQUIRED", f"{missing}不能为空。", 422)
                    # 供应商只维护商品目录，不写库存估值成本；否则 Odoo 会触发库存移动估值权限校验。
                    values.pop("standard_price", None)
            elif create and values.get("brew_item_type") not in {"semi_finished", "finished_product"}:
                raise ApiError("VALIDATION_FORMAT", "产品类型必须是半成品或成品。", 422)
            if values.get("brew_item_type") == "finished_product":
                category_id = values.get("brew_beer_category_id")
                subcategory_id = values.get("brew_beer_subcategory_id")
                if not category_id or not subcategory_id:
                    raise ApiError("VALIDATION_REQUIRED", "成品啤酒必须选择啤酒大类和小类。", 422)
                category_model = self.env["brew.beer.category"].sudo()
                category = category_model.search([("id", "=", category_id), ("active", "=", True), ("parent_id", "=", False)], limit=1)
                subcategory = category_model.search([("id", "=", subcategory_id), ("active", "=", True), ("parent_id", "=", category_id)], limit=1)
                if not category or not subcategory:
                    raise ApiError("VALIDATION_FORMAT", "啤酒小类必须属于所选啤酒大类。", 422)
            elif values.get("brew_item_type") == "semi_finished":
                values["brew_beer_category_id"] = False
                values["brew_beer_subcategory_id"] = False
        elif resource == "suppliers":
            values.update({"company_id": self.company.id, "supplier_rank": 1})
            if create:
                required = {
                    "name": "供应商名称",
                    "brew_contact_name": "联系人",
                    "phone": "联系电话",
                    "email": "电子邮箱",
                    "street": "地址",
                    "brew_social_credit_code": "统一社会信用代码",
                    "brew_bank_account_name": "账户名",
                    "brew_bank_name": "银行名称",
                    "brew_bank_branch": "分行名称",
                    "brew_bank_account": "银行账号",
                }
                missing = next((label for key, label in required.items() if not str(values.get(key) or "").strip()), None)
                if missing:
                    raise ApiError("VALIDATION_REQUIRED", f"{missing}不能为空。", 422)
        elif resource == "customers":
            values.update({"company_id": self.company.id, "customer_rank": 1})
        elif resource not in {"packaging-specs", "material-categories", "beer-categories"}:
            values["company_id"] = self.company.id

        if resource == "locations" and create and not values.get("location_id"):
            factory = self._factory(values["brew_factory_id"])
            if not factory.stock_warehouse_id:
                raise ApiError("STATE_PRECONDITION_FAILED", "厂区尚未配置标准仓库。", 422)
            values["location_id"] = factory.stock_warehouse_id.lot_stock_id.id
            values.setdefault("usage", "internal")
        if resource == "locations" and values.get("location_id"):
            factory_id = values.get("brew_factory_id")
            if not factory_id:
                raise ApiError("VALIDATION_REQUIRED", "所属厂区不能为空。", 422)
            factory = self._factory(factory_id)
            parent = self.env["stock.location"].sudo().search(
                [
                    ("id", "=", values["location_id"]),
                    ("company_id", "=", self.company.id),
                    ("brew_factory_id", "=", factory.id),
                    ("usage", "=", "internal"),
                ],
                limit=1,
            )
            warehouses = self.env["stock.warehouse"].sudo().search(
                [("company_id", "=", self.company.id), ("brew_factory_id", "=", factory.id)]
            )
            if not parent or parent not in warehouses.mapped("lot_stock_id"):
                raise ApiError("DATA_SCOPE_DENIED", "库位必须归属当前厂区的仓库。", 403)
            warehouse = warehouses.filtered(lambda item: item.lot_stock_id == parent)[:1]
            if (warehouse.brew_warehouse_type == "material_issue") != (values.get("brew_location_type") == "material_issue"):
                raise ApiError("VALIDATION_RELATION", "领料库只能设置领料库位，领料库位也必须属于领料库。", 422)
        if resource == "tanks" and values.get("maintenance_equipment_id"):
            factory_id = values.get("factory_id")
            equipment = self.env["maintenance.equipment"].sudo().search(
                [
                    ("id", "=", values["maintenance_equipment_id"]),
                    ("company_id", "=", self.company.id),
                    ("brew_factory_id", "=", factory_id),
                    ("active", "=", True),
                ],
                limit=1,
            )
            if not equipment:
                raise ApiError("DATA_SCOPE_DENIED", "罐体关联设备不属于所选厂区或已停用。", 403)
        if resource == "packaging-specs" and values.get("product_id"):
            product = self.env["product.product"].sudo().search(
                [("id", "=", values["product_id"]), ("product_tmpl_id.company_id", "=", self.company.id)],
                limit=1,
            )
            if not product:
                raise ApiError("DATA_SCOPE_DENIED", "包装规格关联的产品不属于当前企业。", 403)
            if product.brew_item_type != "finished_product":
                raise ApiError("VALIDATION_RELATION", "包装规格只能关联成品酒款。", 422)
        if resource == "packaging-specs":
            if create:
                values["company_id"] = self.company.id
            if "barcode" in values or create:
                values["barcode"] = str(values.get("barcode") or "").strip()
                if len(values["barcode"]) > 128:
                    raise ApiError("VALIDATION_REQUIRED", "包装条码最多 128 字。", 422)
                if create and values["barcode"] and self.env["product.packaging"].sudo().with_context(active_test=False).search_count([("barcode", "=", values["barcode"])]):
                    raise ApiError("VALIDATION_DUPLICATE", "该包装条码已使用，请勿重复分配。", 422)
                values["barcode"] = values["barcode"] or False
            for key in ("brew_volume_l", "qty"):
                if key in values or create:
                    try:
                        number = float(values.get(key) or 0)
                        if not math.isfinite(number) or number <= 0 or key == "qty" and not number.is_integer():
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise ApiError("VALIDATION_RANGE", "单件容量须大于零，每件容器数须为正整数。", 422)
                    values[key] = number
            if create and values.get("brew_barcode_level") not in {"bottle", "can", "keg", "box"}:
                raise ApiError("VALIDATION_REQUIRED", "请选择固定包装类型。", 422)
        if create and resource in {"products", "materials"} and values.get("default_code"):
            duplicate = self.env["product.template"].sudo().with_context(active_test=False).search_count(
                [("company_id", "=", self.company.id), ("default_code", "=", values["default_code"])]
            )
            if duplicate:
                raise ApiError("VALIDATION_DUPLICATE", "产品或物料编码已存在。", 422)
        if resource == "material-categories" and values.get("brew_packaging_type"):
            from .packaging import TYPES
            if values["brew_packaging_type"] not in TYPES:
                raise ApiError("VALIDATION_FORMAT", "包装表单类型无效。", 422)
        if resource in {"material-categories", "beer-categories"}:
            label = "原料种类" if resource == "material-categories" else "啤酒分类"
            model_name = "brew.material.category" if resource == "material-categories" else "brew.beer.category"
            if create and (not str(values.get("name") or "").strip() or not str(values.get("code") or "").strip()):
                raise ApiError("VALIDATION_REQUIRED", f"{label}名称和编码不能为空。", 422)
            if values.get("parent_id"):
                parent = self.env[model_name].sudo().search([("id", "=", values["parent_id"]), ("active", "=", True), ("parent_id", "=", False)], limit=1)
                if not parent:
                    raise ApiError("RESOURCE_NOT_FOUND", f"上级{label}不存在。", 404)
                if resource == "material-categories":
                    values["material_group"] = parent.material_group
            elif create and resource == "material-categories":
                values["material_group"] = values.get("code")
            if create and self.env[model_name].sudo().search_count([("code", "=", values.get("code"))]):
                raise ApiError("VALIDATION_DUPLICATE", f"{label}编码已存在。", 422)
        if create and resource in {"suppliers", "customers"}:
            duplicate = self.env["res.partner"].sudo().with_context(active_test=False).search_count(
                [("company_id", "=", self.company.id), ("name", "=ilike", values.get("name"))]
            )
            if duplicate:
                raise ApiError("VALIDATION_DUPLICATE", "当前企业已存在同名客户或供应商。", 422)
        if create and not str(values.get("name") or "").strip():
            raise ApiError("VALIDATION_REQUIRED", "名称不能为空。", 422)
        return values

    def _sync_equipment_tanks(self, equipment, payload):
        if equipment.brew_equipment_type not in {"mash", "fermentation", "bright"} or "tank_specs" not in payload:
            return
        specs = payload.get("tank_specs") or []
        if not isinstance(specs, list):
            raise ApiError("VALIDATION_FORMAT", "罐体配置必须是数组。", 422)
        factory = equipment.brew_factory_id
        tank_warehouse = self._ensure_tank_warehouse(factory)
        equipment_type = equipment.brew_equipment_type
        tank_model = self.env["brew.tank"].sudo()
        existing = tank_model.search([("maintenance_equipment_id", "=", equipment.id)], order="id")
        seen_ids = set()
        for index, spec in enumerate(specs, start=1):
            if not isinstance(spec, dict):
                raise ApiError("VALIDATION_FORMAT", "罐体配置格式无效。", 422)
            name = str(spec.get("name") or "").strip()
            location_name = str(spec.get("location_name") or "").strip()
            spec_tank_type = str(spec.get("tank_type") or equipment_type)
            if equipment_type == "mash":
                if spec_tank_type not in {"fermentation", "bright"}:
                    raise ApiError("VALIDATION_FORMAT", "酿造系统下的罐体类型必须是发酵罐或清酒罐。", 422)
                tank_type = spec_tank_type
            if not location_name:
                location_name = f"{factory.name}-{equipment.name}-{name}"
            capacity = float(spec.get("capacity_l") or equipment.brew_tank_volume_l or equipment.brew_capacity_l or 0)
            if not name or capacity <= 0:
                raise ApiError("VALIDATION_REQUIRED", "罐号和罐体积不能为空。", 422)
            location = self.env["stock.location"].sudo().search(
                [
                    ("company_id", "=", self.company.id),
                    ("brew_factory_id", "=", factory.id),
                    ("name", "=", location_name),
                    ("location_id", "child_of", tank_warehouse.view_location_id.id),
                    ("usage", "=", "internal"),
                ], limit=1
            )
            if not location:
                location = self.env["stock.location"].sudo().create(
                    {
                        "name": location_name,
                        "location_id": tank_warehouse.lot_stock_id.id,
                        "usage": "internal",
                        "company_id": self.company.id,
                        "brew_factory_id": factory.id,
                        "brew_location_type": "semi_finished",
                    }
                )
            tank = tank_model.search(
                [("maintenance_equipment_id", "=", equipment.id), ("name", "=", name)], limit=1
            )
            values = {
                "name": name,
                "factory_id": factory.id,
                "maintenance_equipment_id": equipment.id,
                "stock_location_id": location.id,
                "tank_type": spec_tank_type,
                "capacity_l": capacity,
                "active": True,
            }
            if tank:
                tank.write(values)
            else:
                conflict = tank_model.search(
                    [
                        ("factory_id", "=", factory.id),
                        ("name", "=", name),
                        ("id", "not in", list(existing.ids)),
                    ], limit=1
                )
                if conflict:
                    if conflict.maintenance_equipment_id and not conflict.maintenance_equipment_id.active:
                        # 旧设备已停用时复用其罐体记录，避免唯一罐号约束造成数据库异常。
                        tank = conflict
                        tank.write(values)
                    else:
                        raise ApiError("VALIDATION_DUPLICATE", f"厂区内罐号 {name} 已存在，请更换罐号。", 422)
                else:
                    tank = tank_model.create(values)
            seen_ids.add(tank.id)
        stale = existing.filtered(lambda item: item.id not in seen_ids)
        if stale:
            stale.write({"active": False})

    def _ensure_tank_warehouse(self, factory):
        """每个厂区固定一个罐体仓，发酵罐/清酒罐以库位形式归档到这里。"""
        warehouse_model = self.env["stock.warehouse"].sudo()
        warehouse = warehouse_model.search(
            [
                ("company_id", "=", self.company.id),
                ("brew_factory_id", "=", factory.id),
                ("brew_warehouse_type", "=", "tank"),
            ], limit=1
        )
        if warehouse:
            return warehouse
        return warehouse_model.create(
            {
                "name": f"{factory.name}罐体仓",
                "code": self._available_warehouse_code(f"{factory.code}-TANK"),
                "company_id": self.company.id,
                "brew_factory_id": factory.id,
                "brew_warehouse_type": "tank",
            }
        )

    def _ensure_tank_location(self, tank):
        """Bind a tank to its own location without posting any inventory."""
        tank.ensure_one()
        if tank.company_id != self.company:
            raise ApiError("DATA_SCOPE_DENIED", "罐体不属于当前企业。", 403)
        self._factory(tank.factory_id.id)
        self.env.cr.execute("SELECT id FROM brew_tank WHERE id = %s FOR UPDATE", [tank.id])
        tank.invalidate_recordset(["stock_location_id"])
        if tank.stock_location_id:
            location = tank.stock_location_id
            if location.company_id != self.company or location.brew_factory_id != tank.factory_id or location.usage != "internal" or location.brew_location_type != "semi_finished":
                raise ApiError("VALIDATION_RELATION", "罐体须关联本厂区的半成品库位。", 422)
            if self.env["brew.tank"].sudo().search_count([("stock_location_id", "=", location.id), ("id", "!=", tank.id)]):
                raise ApiError("VALIDATION_RELATION", "该库位已关联其他罐体。", 422)
            return location
        warehouse = self._ensure_tank_warehouse(tank.factory_id)
        locations = self.env["stock.location"].sudo()
        name = tank.name
        suffix = 1
        while locations.with_context(active_test=False).search_count([("location_id", "=", warehouse.lot_stock_id.id), ("name", "=", name)]):
            suffix += 1
            name = f"{tank.name}（罐位 {suffix}）"
        location = locations.create({"name": name, "company_id": self.company.id,
                                     "location_id": warehouse.lot_stock_id.id,
                                     "brew_factory_id": tank.factory_id.id,
                                     "usage": "internal", "brew_location_type": "semi_finished"})
        tank.stock_location_id = location
        return location

    def _available_warehouse_code(self, preferred):
        base = (preferred or "BREW").upper()[:5]
        code = base
        suffix = 1
        model = self.env["stock.warehouse"].sudo()
        while model.search_count([("company_id", "=", self.company.id), ("code", "=", code)]):
            suffix += 1
            code = f"{base[: max(1, 5 - len(str(suffix)))]}{suffix}"
        return code

    def create(self, resource, payload):
        self.ensure_write_access(resource, create=True)
        values = self._values(resource, payload, create=True)
        if resource == "warehouses":
            values["code"] = self._available_warehouse_code(values.get("code"))
        record = self.env[self._config(resource)["model"]].sudo().create(values)
        if resource == "tanks":
            self._ensure_tank_location(record)
        if resource == "suppliers":
            if not record.brew_supplier_code:
                record.brew_supplier_code = f"SUP-{record.id:06d}"
            self._save_supplier_account(record, payload)
        if resource == "equipment":
            self._sync_equipment_tanks(record, payload)
        if resource == "factories" and not record.stock_warehouse_id:
            warehouse = self.env["stock.warehouse"].sudo().create(
                {
                    "name": record.name,
                    "code": self._available_warehouse_code(record.code),
                    "company_id": self.company.id,
                    "brew_factory_id": record.id,
                }
            )
            record.stock_warehouse_id = warehouse
            self._ensure_tank_warehouse(record)
        elif resource == "factories":
            self._ensure_tank_warehouse(record)
        return record, self.serialize(resource, record)

    def _save_supplier_account(self, partner, payload):
        login = str(payload.get("supplier_account_login") or "").strip()
        password = str(payload.get("supplier_account_password") or "")
        user_model = self.env["res.users"].sudo().with_context(active_test=False)
        account = user_model.search([("brew_supplier_id", "=", partner.id)], limit=1)

        if not account and not login and not password:
            return
        if not account and (not login or len(password) < 8):
            raise ApiError("VALIDATION_REQUIRED", "供应商账号和初始密码必须同时填写，密码至少 8 位。", 422)
        if account and not login:
            login = account.login
        if account and password and len(password) < 8:
            raise ApiError("VALIDATION_FORMAT", "新密码至少 8 位。", 422)

        duplicate_domain = [("login", "=", login)]
        if account:
            duplicate_domain.append(("id", "!=", account.id))
        if user_model.search_count(duplicate_domain):
            raise ApiError("VALIDATION_DUPLICATE", "供应商账号已存在。", 422)
        if account:
            account_values = {"login": login, "name": partner.name}
            if password:
                account_values["password"] = password
            account.write(account_values)
            return

        internal_group = self.env.ref("base.group_user")
        supplier_group = self.env.ref("craft_brew_core.group_brew_supplier")
        user_model.with_context(no_reset_password=True).create({
            "name": partner.name,
            "login": login,
            "password": password,
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "groups_id": [(6, 0, [internal_group.id, supplier_group.id])],
            "brew_supplier_id": partner.id,
        })

    def update(self, resource, record_id, payload):
        self.ensure_write_access(resource)
        record = self._record(resource, record_id)
        expected = payload.get("version")
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        values = self._values(resource, payload)
        if resource == "packaging-specs":
            if values.get("barcode") and self.env["product.packaging"].sudo().with_context(active_test=False).search_count([
                    ("barcode", "=", values["barcode"]), ("id", "!=", record.id)]):
                raise ApiError("VALIDATION_DUPLICATE", "该包装条码已使用，请勿重复分配。", 422)
            from .product_trace import ProductTrace
            plans = self.env["brew.production.plan"].sudo().search([
                ("company_id", "=", self.company.id), ("product_tmpl_id", "=", record.product_id.product_tmpl_id.id)])
            used = self.env["brew.packaging.order"].sudo().search_count([("packaging_id", "=", record.id)]) or any(ProductTrace.plan_package(p) == str(record.id) for p in plans)
            identity = {"barcode", "product_id", "brew_barcode_level", "brew_volume_l", "qty"}
            if not record.barcode:
                identity.discard("barcode")
            if used and any((record[key].id if key == "product_id" else record[key]) != values[key] for key in identity & values.keys()):
                raise ApiError("STATE_PRECONDITION_FAILED", "已用于生产的包装条码和规格不能更改，请新增包装规格。", 422)
        if resource == "warehouses" and "brew_warehouse_type" in values and values["brew_warehouse_type"] != record.brew_warehouse_type and "material_issue" in {values["brew_warehouse_type"], record.brew_warehouse_type}:
            raise ApiError("STATE_PRECONDITION_FAILED", "领料库类型不可与其他仓库类型互相更改，请新建仓库。", 422)
        if resource == "locations" and "brew_location_type" in values:
            warehouse = self.env["stock.warehouse"].sudo().search([("lot_stock_id", "=", record.location_id.id), ("company_id", "=", self.company.id)], limit=1)
            if warehouse and (warehouse.brew_warehouse_type == "material_issue") != (values["brew_location_type"] == "material_issue"):
                raise ApiError("VALIDATION_RELATION", "领料库只能设置领料库位，领料库位也必须属于领料库。", 422)
        has_supplier_account_update = resource == "suppliers" and (
            "supplier_account_login" in payload or "supplier_account_password" in payload
        )
        if not values and not has_supplier_account_update:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        if values:
            record.write(values)
        if resource == "tanks":
            self._ensure_tank_location(record)
        if resource == "suppliers" and has_supplier_account_update:
            self._save_supplier_account(record, payload)
        if resource == "equipment":
            self._sync_equipment_tanks(record, payload)
        return record, self.serialize(resource, record)

    def set_active(self, resource, record_id, active, version):
        self.ensure_write_access(resource)
        record = self._record(resource, record_id)
        if resource == "warehouses" and record.brew_warehouse_type == "tank" and not active:
            raise ApiError("STATE_ACTION_NOT_ALLOWED", "罐体仓由系统维护，不允许删除或停用。", 422)
        if "active" not in record._fields:
            raise ApiError("STATE_ACTION_NOT_ALLOWED", "该资源不支持归档。", 422)
        current = record.write_date.isoformat() if record.write_date else None
        if not version or version != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        record.write({"active": active})
        if resource == "equipment":
            self.env["brew.tank"].sudo().search([("maintenance_equipment_id", "=", record.id)]).write({"active": active})
        return record, self.serialize(resource, record)

    def _many2one(self, record, field_name):
        value = record[field_name]
        return {"id": str(value.id), "name": value.display_name} if value else None

    def serialize(self, resource, record):
        data = {
            "id": str(record.id),
            "name": record.display_name,
            "active": record.active if "active" in record._fields else True,
            "version": record.write_date.isoformat() if record.write_date else None,
        }
        if resource == "tanks":
            data.update(status=record.status, current_volume_l=record.current_volume_l,
                        occupied=bool(record.current_volume_l > 0 or record.current_production_batch_id or record.status != 'empty'))
        if resource == "products":
            data["product_name"] = record.name
            data["product_variant_id"] = str(record.product_variant_id.id)
        if resource == "packaging-specs":
            data["product_tmpl_id"] = str(record.product_id.product_tmpl_id.id)
            data["product_name"] = record.product_id.product_tmpl_id.name
        field_names = self._config(resource)["fields"]
        for field_name in field_names:
            if field_name == "name" or field_name not in record._fields:
                continue
            if resource == "materials" and field_name == "image_1920":
                continue
            field = record._fields[field_name]
            value = record[field_name]
            if field.type == "many2one":
                data[field_name] = self._many2one(record, field_name)
            elif field.type in {"date", "datetime"}:
                data[field_name] = value.isoformat() if value else None
            else:
                data[field_name] = value
        if resource == "equipment":
            data["tank_specs"] = [
                {
                    "id": str(tank.id),
                    "name": tank.name,
                    "tank_type": tank.tank_type,
                    "capacity_l": tank.capacity_l,
                    "location_id": str(tank.stock_location_id.id) if tank.stock_location_id else None,
                    "location_name": tank.stock_location_id.name if tank.stock_location_id else "",
                    "active": tank.active,
                }
                for tank in self.env["brew.tank"].sudo().search(
                    [("maintenance_equipment_id", "=", record.id), ("active", "=", True)], order="name,id"
                )
            ]
        if resource == "locations":
            data["location_name"] = record.name
            data["complete_name"] = record.complete_name
            data["location_code"] = f"LOC-{record.id:06d}"
            data["created_at"] = record.create_date.isoformat() if record.create_date else None
            tank = self.env["brew.tank"].sudo().with_context(active_test=False).search(
                [("stock_location_id", "=", record.id)], limit=1
            )
            if tank:
                data["tank"] = {
                    "id": str(tank.id),
                    "name": tank.name,
                    "tank_type": tank.tank_type,
                    "capacity_l": tank.capacity_l,
                    "status": tank.status,
                    "equipment_id": str(tank.maintenance_equipment_id.id) if tank.maintenance_equipment_id else None,
                    "equipment_name": tank.maintenance_equipment_id.name if tank.maintenance_equipment_id else "",
                }
        if resource in {"factories", "equipment", "tanks", "warehouses", "locations", "products", "materials", "suppliers", "customers"}:
            data["company_id"] = str(self.company.id)
        if resource in {"products", "materials"}:
            data["product_variant_id"] = str(record.product_variant_id.id)
            data["weight_uom"] = "kg" if record.uom_id == self.env.ref("uom.product_uom_kgm") else "g" if record.uom_id == self.env.ref("uom.product_uom_gram") else ""
            data["is_weight_uom"] = record.uom_id.category_id == self.env.ref("uom.product_uom_kgm").category_id
            data["supplier"] = {"id": str(record.brew_supplier_id.id), "name": record.brew_supplier_id.name} if record.brew_supplier_id else None
        if resource == "suppliers":
            account = self.env["res.users"].sudo().with_context(active_test=False).search([("brew_supplier_id", "=", record.id)], limit=1)
            data["account"] = {"login": account.login, "active": account.active} if account else None
        if resource == "material-categories":
            data["brew_packaging_type"] = record.brew_packaging_type
        if resource == "materials":
            data["image_url"] = f"/api/v1/materials/{record.id}/image" if record.image_1920 else None
        return data

    def material_image(self, record_id):
        record = self._record("materials", record_id)
        if not record.image_256:
            raise ApiError("RESOURCE_NOT_FOUND", "商品图片不存在。", 404)
        content = base64.b64decode(record.image_256)
        with Image.open(BytesIO(content)) as image:
            mimetype = image.get_format_mimetype() or "image/jpeg"
        return content, mimetype

    def _equipment(self, equipment_id):
        equipment = self.env["maintenance.equipment"].sudo().search(
            [
                ("id", "=", int(equipment_id)),
                ("company_id", "=", self.company.id),
                ("brew_factory_id", "!=", False),
                ("active", "=", True),
            ],
            limit=1,
        )
        if not equipment:
            raise ApiError("RESOURCE_NOT_FOUND", "设备不存在或已停用。", 404)
        return equipment

    def list_unavailability(self, equipment_id):
        equipment = self._equipment(equipment_id)
        records = self.env["brew.equipment.unavailability"].sudo().search(
            [("equipment_id", "=", equipment.id)], order="start_at,id"
        )
        return [self.serialize_unavailability(item) for item in records]

    def create_unavailability(self, equipment_id, payload):
        self.ensure_write_access("equipment")
        equipment = self._equipment(equipment_id)
        name = str(payload.get("name") or "").strip()
        start_at = payload.get("start_at")
        end_at = payload.get("end_at")
        if not name or not start_at or not end_at:
            raise ApiError("VALIDATION_REQUIRED", "名称、开始时间和结束时间不能为空。", 422)
        record = self.env["brew.equipment.unavailability"].sudo().create(
            {
                "name": name,
                "equipment_id": equipment.id,
                "start_at": start_at,
                "end_at": end_at,
                "reason": payload.get("reason"),
            }
        )
        return record, self.serialize_unavailability(record)

    def update_unavailability(self, record_id, payload):
        self.ensure_write_access("equipment")
        record = self.env["brew.equipment.unavailability"].sudo().search(
            [("id", "=", int(record_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "设备不可用时段不存在。", 404)
        current = record.write_date.isoformat() if record.write_date else None
        if not payload.get("version") or payload["version"] != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        if payload.get("action") == "cancel":
            record.action_cancel()
        else:
            values = {
                key: payload[key]
                for key in {"name", "start_at", "end_at", "reason"}
                if key in payload
            }
            if not values:
                raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
            record.write(values)
        return record, self.serialize_unavailability(record)

    def serialize_unavailability(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "equipment_id": str(record.equipment_id.id),
            "factory_id": str(record.factory_id.id),
            "start_at": record.start_at.isoformat() if record.start_at else None,
            "end_at": record.end_at.isoformat() if record.end_at else None,
            "reason": record.reason,
            "state": record.state,
            "version": record.write_date.isoformat() if record.write_date else None,
        }
