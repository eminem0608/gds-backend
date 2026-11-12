{
    "name": "Craft Brew Inventory",
    "summary": "Lot quality, FEFO reservation and controlled inventory documents.",
    "version": "17.0.1.2.0",
    "category": "Inventory",
    "author": "Craft Brew",
    "license": "LGPL-3",
    "depends": ["craft_brew_planning", "stock"],
    "data": ["security/ir.model.access.csv", "security/record_rules.xml", "data/quantity_precision.xml", "data/ir_sequence.xml", "data/ir_cron.xml", "views/inventory_views.xml"],
    "installable": True,
}
