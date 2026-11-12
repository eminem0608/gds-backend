from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cny = env.ref("base.CNY", raise_if_not_found=False)
    if not cny:
        raise RuntimeError("The CNY currency is missing from the database.")
    if not cny.active:
        cny.write({"active": True})

    env["purchase.order"].sudo().search(
        [
            ("company_id", "=", env.company.id),
            ("brew_factory_id", "!=", False),
        ]
    ).write({"currency_id": cny.id})
