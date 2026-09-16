def _warehouse_code(env, company, preferred):
    base = (preferred or "BREW").upper()[:5]
    code = base
    suffix = 1
    while env["stock.warehouse"].sudo().search_count(
        [("company_id", "=", company.id), ("code", "=", code)]
    ):
        suffix += 1
        code = f"{base[: max(1, 5 - len(str(suffix)))]}{suffix}"
    return code


def post_init_hook(env):
    """Create standard-model counterparts without deleting legacy records."""
    factories = env["brew.factory"].sudo().with_context(active_test=False).search([])
    for factory in factories:
        warehouse = factory.stock_warehouse_id
        if not warehouse:
            warehouse = env["stock.warehouse"].sudo().create(
                {
                    "name": factory.name,
                    "code": _warehouse_code(env, factory.company_id, factory.code),
                    "company_id": factory.company_id.id,
                    "brew_factory_id": factory.id,
                }
            )
            factory.stock_warehouse_id = warehouse

    for legacy in env["brew.warehouse"].sudo().with_context(active_test=False).search([]):
        if legacy.stock_location_id:
            continue
        parent = legacy.factory_id.stock_warehouse_id.lot_stock_id
        legacy.stock_location_id = env["stock.location"].sudo().create(
            {
                "name": legacy.name,
                "location_id": parent.id,
                "usage": "internal",
                "company_id": legacy.company_id.id,
                "brew_factory_id": legacy.factory_id.id,
                "brew_location_type": legacy.warehouse_type,
                "brew_legacy_warehouse_id": legacy.id,
                "active": legacy.active,
            }
        )

    for legacy in env["brew.equipment"].sudo().with_context(active_test=False).search([]):
        if legacy.maintenance_equipment_id:
            continue
        legacy.maintenance_equipment_id = env["maintenance.equipment"].sudo().create(
            {
                "name": legacy.name,
                "company_id": legacy.company_id.id,
                "brew_factory_id": legacy.factory_id.id,
                "brew_equipment_type": legacy.equipment_type,
                "brew_capacity_l": legacy.capacity_l,
                "brew_status": legacy.status,
                "brew_legacy_equipment_id": legacy.id,
                "active": legacy.active,
            }
        )

    for tank in env["brew.tank"].sudo().with_context(active_test=False).search([]):
        if tank.equipment_id and not tank.maintenance_equipment_id:
            tank.maintenance_equipment_id = tank.equipment_id.maintenance_equipment_id

    for legacy in env["brew.supplier"].sudo().with_context(active_test=False).search([]):
        if legacy.partner_id:
            continue
        legacy.partner_id = env["res.partner"].sudo().create(
            {
                "name": legacy.name,
                "company_id": legacy.company_id.id,
                "supplier_rank": 1,
                "phone": legacy.phone,
                "street": legacy.address,
                "brew_supply_category": legacy.supply_category,
                "brew_legacy_supplier_id": legacy.id,
                "active": legacy.active,
            }
        )

    for legacy in env["brew.customer"].sudo().with_context(active_test=False).search([]):
        if legacy.partner_id:
            continue
        legacy.partner_id = env["res.partner"].sudo().create(
            {
                "name": legacy.name,
                "company_id": legacy.company_id.id,
                "customer_rank": 1,
                "phone": legacy.phone,
                "street": legacy.delivery_address,
                "brew_customer_level": legacy.customer_level,
                "brew_legacy_customer_id": legacy.id,
                "active": legacy.active,
            }
        )

    for legacy in env["brew.material"].sudo().with_context(active_test=False).search([]):
        if legacy.product_tmpl_id:
            continue
        legacy.product_tmpl_id = env["product.template"].sudo().create(
            {
                "name": legacy.name,
                "company_id": legacy.company_id.id,
                "detailed_type": "product",
                "purchase_ok": True,
                "sale_ok": False,
                "tracking": "lot",
                "list_price": legacy.price,
                "brew_item_type": "packaging_material" if legacy.category == "packaging" else "raw_material",
                "brew_material_category": legacy.category,
                "brew_brand": legacy.brand,
                "brew_specification": legacy.specification,
                "brew_shelf_life_days": legacy.shelf_life_days,
                "brew_extract_rate": legacy.extract_rate,
                "brew_color_ebc": legacy.color_ebc,
                "brew_alpha_acid": legacy.alpha_acid,
                "brew_flavor_desc": legacy.flavor_desc,
                "brew_legacy_material_id": legacy.id,
                "active": legacy.active,
            }
        )
