def migrate(cr, version):
    cr.execute("ALTER TABLE brew_product_demand DROP CONSTRAINT IF EXISTS brew_product_demand_package_qty_positive")
