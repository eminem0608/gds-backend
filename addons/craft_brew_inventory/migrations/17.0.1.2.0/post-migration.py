def migrate(cr, version):
    # Legacy "counting" lines were initialized to their book quantity, so they
    # cannot be assumed physically counted. Preserve their values for review.
    # Only previously submitted/completed records have confirmed count intent.
    cr.execute("""UPDATE brew_inventory_count_line line SET is_counted = true
        FROM brew_inventory_count count WHERE line.count_id = count.id
        AND count.state IN ('pending_approval', 'done')""")
    cr.execute("UPDATE brew_inventory_count SET owner_id = create_uid WHERE create_uid IS NOT NULL")
