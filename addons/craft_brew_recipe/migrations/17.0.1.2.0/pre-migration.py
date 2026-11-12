def migrate(cr, version):
    # A product can have several distinct recipes for the same brewing system.
    cr.execute(
        "ALTER TABLE brew_recipe DROP CONSTRAINT IF EXISTS brew_recipe_company_product_equipment_unique"
    )
