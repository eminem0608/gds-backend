def migrate(cr, version):
    # Recipes used to be unique by product alone. A beer can now have a
    # separate recipe for each brewing system.
    cr.execute("ALTER TABLE brew_recipe DROP CONSTRAINT IF EXISTS brew_recipe_company_product_unique")
    cr.execute("ALTER TABLE brew_recipe ADD COLUMN IF NOT EXISTS equipment_id integer")

    # Preserve the equipment already stored in each recipe version's design.
    cr.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (recipe_id)
                   recipe_id,
                   NULLIF(design_json::jsonb ->> 'brew_equipment_id', '')::integer AS equipment_id
              FROM brew_recipe_version
             ORDER BY recipe_id, version DESC, id DESC
        )
        UPDATE brew_recipe AS recipe
           SET equipment_id = latest.equipment_id
          FROM latest
         WHERE recipe.id = latest.recipe_id
           AND latest.equipment_id IS NOT NULL
        """
    )
