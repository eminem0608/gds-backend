{
    "name": "Craft Brew Production Execution",
    "summary": "Material issue/return and traceable brewery production execution.",
    "version": "17.0.1.0.0",
    "category": "Manufacturing",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["craft_brew_purchase"],
    "data": [
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/ir_sequence.xml",
        "views/production_views.xml",
    ],
    "installable": True,
}
