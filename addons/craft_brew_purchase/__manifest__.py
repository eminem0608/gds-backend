{
    "name": "Craft Brew Purchase and Quality",
    "summary": "Shortage requisitions, standard purchase orders, receipts and incoming quality.",
    "version": "17.0.6.0.0",
    "category": "Purchase",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["craft_brew_inventory", "purchase"],
    "data": [
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/ir_sequence.xml",
        "views/purchase_views.xml",
    ],
    "installable": True,
}
