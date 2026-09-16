{
    "name": "Craft Brew Fermentation",
    "summary": "Configurable fermentation sessions, readings, controlled actions and anomalies.",
    "version": "17.0.1.0.0",
    "category": "Manufacturing",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["craft_brew_production"],
    "data": [
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        "views/fermentation_views.xml",
    ],
    "installable": True,
}
