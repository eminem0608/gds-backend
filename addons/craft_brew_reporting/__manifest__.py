{
    "name": "Craft Brew Reporting and Notifications",
    "summary": "Operational dashboards, controlled exports and in-app notifications.",
    "version": "17.0.1.0.0",
    "category": "Manufacturing",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["craft_brew_trace"],
    "data": [
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        "views/reporting_views.xml",
    ],
    "installable": True,
}
