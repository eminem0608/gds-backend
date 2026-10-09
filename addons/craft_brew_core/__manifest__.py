{
    "name": "Craft Brew Core",
    "summary": "Core menus, security groups, and shared settings for craft beer SaaS.",
    "version": "17.0.2.0.0",
    "category": "Manufacturing",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/ir_cron.xml",
        "views/menu_views.xml",
        "views/account_views.xml",
    ],
    "application": True,
    "installable": True,
}
