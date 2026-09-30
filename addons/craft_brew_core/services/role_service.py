ROLE_DEFINITIONS = {
    "general_manager": {
        "xml_id": "craft_brew_core.group_brew_general_manager",
        "label": "总经理",
        "default_page": "dashboard",
        "permissions": {
            "dashboard.overview.read",
            "dashboard.tanks.read",
            "dashboard.plans.read",
            "dashboard.inventory.read",
            "plan.production.read",
            "plan.production.approve",
            "purchase.order.read",
            "purchase.order.approve",
            "trace.batch.read",
            "report.all.read",
            "report.all.export",
            "base.beer_category.manage",
        },
    },
    "production_director": {
        "xml_id": "craft_brew_core.group_brew_production_director",
        "label": "生产总监",
        "default_page": "factory",
        "permissions": {
            "dashboard.tanks.read",
            "dashboard.plans.read",
            "recipe.version.approve",
            "demand.product.confirm",
            "plan.production.manage",
            "plan.production.approve",
            "plan.production.dispatch",
            "production.record.approve",
            "production.anomaly.manage",
            "base.equipment.manage",
            "base.product.manage",
            "purchase.requisition.approve",
            "trace.production.read",
            "report.production.read",
        },
    },
    "brewer": {
        "xml_id": "craft_brew_core.group_brew_brewer",
        "label": "酿酒师",
        "default_page": "recipe",
        "permissions": {
            "recipe.version.manage",
        },
    },
    "operator": {
        "xml_id": "craft_brew_core.group_brew_operator",
        "label": "操作工",
        "default_page": "tasks",
        "permissions": {
            "task.own.read",
            "task.own.execute",
            "production.record.create",
            "production.fermentation.record",
            "production.anomaly.create",
            "inventory.requisition.acknowledge",
            "inventory.return.create",
        },
    },
    "warehouse_keeper": {
        "xml_id": "craft_brew_core.group_brew_warehouse",
        "label": "库管",
        "default_page": "inventory",
        "permissions": {
            "dashboard.inventory.read",
            "inventory.stock.read",
            "inventory.receipt.manage",
            "inventory.quality.manage",
            "inventory.requisition.manage",
            "inventory.count.manage",
            "inventory.scrap.manage",
            "inventory.finished_goods.receive",
            "inventory.outbound.confirm",
            "trace.inventory.read",
            "base.location.manage",
        },
    },
    "purchaser": {
        "xml_id": "craft_brew_core.group_brew_purchase",
        "label": "采购",
        "default_page": "inventory",
        "permissions": {
            "purchase.requisition.read",
            "purchase.order.manage",
            "purchase.shipment.manage",
            "base.supplier.manage",
            "base.material.manage",
            "inventory.material.summary.read",
            "report.purchase.read",
            "report.purchase.export",
        },
    },
    "salesperson": {
        "xml_id": "craft_brew_core.group_brew_sales",
        "label": "销售",
        "default_page": "production",
        "permissions": {
            "demand.product.manage_own",
            "inventory.finished_goods.read",
            "outbound.order.manage_own",
            "logistics.shipment.manage_own",
            "trace.finished_goods.read",
            "report.sales.read",
            "base.customer.manage",
        },
    },
    "finance": {
        "xml_id": "craft_brew_core.group_brew_finance",
        "label": "财务",
        "default_page": "reports",
        "permissions": {
            "purchase.order.read",
            "inventory.movement.read",
            "report.purchase.export",
            "report.inbound.export",
            "report.outbound.export",
            "report.inventory.export",
            "export.history.read",
        },
    },
    "tenant_admin": {
        "xml_id": "craft_brew_core.group_brew_admin",
        "label": "系统管理员",
        "default_page": "settings",
        "permissions": {
            "admin.user.manage",
            "admin.role.assign",
            "admin.data_scope.assign",
            "base.master_data.manage",
            "admin.rule.manage",
            "recipe.version.approve",
            "demand.product.confirm",
            "admin.audit.read",
            "admin.integration.monitor",
        },
    },
    "supplier": {
        "xml_id": "craft_brew_core.group_brew_supplier",
        "label": "供应商",
        "default_page": "supplier_catalog",
        "permissions": {
            "supplier.catalog.manage",
        },
    },
}


MENU_DEFINITIONS = (
    ("dashboard", "总数据看板", {"general_manager"}),
    (
        "factory",
        "酒厂",
        {"general_manager", "production_director", "operator", "warehouse_keeper"},
    ),
    ("production", "生产管理", {"general_manager", "production_director", "operator", "salesperson"}),
    ("recipe", "配方工艺", {"general_manager", "production_director", "brewer", "tenant_admin"}),
    (
        "inventory",
        "库存采购",
        {"general_manager", "production_director", "warehouse_keeper", "purchaser", "finance"},
    ),
    (
        "trace",
        "物流追溯",
        {"general_manager", "production_director", "warehouse_keeper", "purchaser", "salesperson"},
    ),
    (
        "reports",
        "报表分析",
        {"general_manager", "production_director", "warehouse_keeper", "purchaser", "salesperson", "finance"},
    ),
    (
        "settings",
        "系统设置",
        {
            "general_manager",
            "production_director",
            "warehouse_keeper",
            "purchaser",
            "salesperson",
            "tenant_admin",
        },
    ),
    (
        "tasks",
        "我的任务",
        {
            "general_manager",
            "production_director",
            "operator",
            "warehouse_keeper",
            "purchaser",
            "salesperson",
            "finance",
            "tenant_admin",
        },
    ),
    ("supplier_catalog", "供应商商品", {"supplier"}),
)


ROLE_PRIORITY = (
    "general_manager",
    "production_director",
    "brewer",
    "operator",
    "warehouse_keeper",
    "purchaser",
    "salesperson",
    "finance",
    "tenant_admin",
    "supplier",
)


def permissions_for_roles(role_codes):
    permissions = set()
    for code in role_codes:
        permissions.update(ROLE_DEFINITIONS.get(code, {}).get("permissions", set()))
    return sorted(permissions)


def menus_for_roles(role_codes):
    role_set = set(role_codes)
    return [
        {"code": code, "label": label}
        for code, label, allowed_roles in MENU_DEFINITIONS
        if role_set.intersection(allowed_roles)
    ]


def menu_codes_for_role(role_code):
    """Return the default menu codes granted by one system role."""
    return [
        code
        for code, _label, allowed_roles in MENU_DEFINITIONS
        if role_code in allowed_roles
    ]


def default_page_for_roles(role_codes):
    role_set = set(role_codes)
    for code in ROLE_PRIORITY:
        if code in role_set:
            return ROLE_DEFINITIONS[code]["default_page"]
    return "tasks"
