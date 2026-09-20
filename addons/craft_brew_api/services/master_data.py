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
        "write_groups": ["craft_brew_core.group_brew_admin"],
        "fields": {"name", "code", "brew_factory_id", "brew_warehouse_type", "lot_stock_id"},
    },
    "locations": {
        "model": "stock.location",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_warehouse",
        ],
        "fields": {"name", "brew_factory_id", "brew_location_type", "location_id", "usage"},
    },
    "products": {
        "model": "product.template",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
        ],
        "fields": {"name", "default_code", "brew_item_type", "brew_brand", "brew_specification", "uom_id", "uom_po_id", "tracking", "standard_price"},
    },
    "materials": {
        "model": "product.template",
        "write_groups": [
            "craft_brew_core.group_brew_admin",
            "craft_brew_core.group_brew_production_director",
            "craft_brew_core.group_brew_purchase",
        ],
        "fields": {"name", "default_code", "brew_item_type", "brew_material_category", "brew_brand", "brew_specification", "uom_id", "uom_po_id", "tracking", "standard_price", "brew_shelf_life_days", "brew_extract_rate", "brew_color_ebc", "brew_alpha_acid", "brew_flavor_desc"},
    },
    "material-categories": {
        "model": "brew.material.category",
        "write_groups": ["craft_brew_core.group_brew_admin"],
        "fields": {"name", "code", "material_group", "parent_id", "sequence"},
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
        "fields": {"name", "phone", "email", "street", "street2", "city", "brew_supply_category"},
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

    def ensure_write_access(self, resource):
        config = self._config(resource)
        if not any(self.user.has_group(group) for group in config["write_groups"]):
            raise ApiError("PERMISSION_DENIED", "当前账号无权维护该主数据。", 403)

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
        if resource in {"factories", "equipment", "tanks", "warehouses", "locations"} and not (
            self.user.has_group("craft_brew_core.group_brew_admin")
            or self.user.has_group("craft_brew_core.group_brew_general_manager")
        ):
            field_name = {
                "factories": "id",
                "equipment": "brew_factory_id",
                "tanks": "factory_id",
                "warehouses": "brew_factory_id",
                "locations": "brew_factory_id",
            }[resource]
            domain += [(field_name, "in", self.user.brew_factory_ids.ids)]
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
        if "active" in model._fields:
            domain += [("active", "=", True if active is None else active)]
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
        return factory

    def _values(self, resource, payload, create=False):
        config = self._config(resource)
        special_fields = {"tank_specs"} if resource == "equipment" else set()
        unknown = set(payload) - config["fields"] - {"version"} - special_fields
        if unknown:
            raise ApiError("VALIDATION_FORMAT", "存在不允许写入的字段。", 422, {"fields": sorted(unknown)})
        values = {key: payload[key] for key in config["fields"] if key in payload}
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
            elif create and values.get("brew_item_type") not in {"semi_finished", "finished_product"}:
                raise ApiError("VALIDATION_FORMAT", "产品类型必须是半成品或成品。", 422)
        elif resource == "suppliers":
            values.update({"company_id": self.company.id, "supplier_rank": 1})
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
        if create and resource in {"products", "materials"} and values.get("default_code"):
            duplicate = self.env["product.template"].sudo().with_context(active_test=False).search_count(
                [("company_id", "=", self.company.id), ("default_code", "=", values["default_code"])]
            )
            if duplicate:
                raise ApiError("VALIDATION_DUPLICATE", "产品或物料编码已存在。", 422)
        if resource in {"material-categories", "beer-categories"}:
            label = "原料种类" if resource == "material-categories" else "啤酒分类"
            model_name = "brew.material.category" if resource == "material-categories" else "brew.beer.category"
            if create and (not str(values.get("name") or "").strip() or not str(values.get("code") or "").strip()):
                raise ApiError("VALIDATION_REQUIRED", f"{label}名称和编码不能为空。", 422)
            if values.get("parent_id"):
                parent = self.env[model_name].sudo().search([("id", "=", values["parent_id"]), ("active", "=", True)], limit=1)
                if not parent:
                    raise ApiError("RESOURCE_NOT_FOUND", f"上级{label}不存在。", 404)
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
                "name": "罐体仓",
                "code": self._available_warehouse_code(f"{factory.code}-TANK"),
                "company_id": self.company.id,
                "brew_factory_id": factory.id,
                "brew_warehouse_type": "tank",
            }
        )

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
        self.ensure_write_access(resource)
        values = self._values(resource, payload, create=True)
        record = self.env[self._config(resource)["model"]].sudo().create(values)
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

    def update(self, resource, record_id, payload):
        self.ensure_write_access(resource)
        record = self._record(resource, record_id)
        expected = payload.get("version")
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        values = self._values(resource, payload)
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        record.write(values)
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
        field_names = self._config(resource)["fields"]
        for field_name in field_names:
            if field_name == "name" or field_name not in record._fields:
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
        return data

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
