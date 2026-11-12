"""Run inside Odoo's container. Default is a rollback preview; --apply commits.

python3 /tmp/repair_tank_locations.py --database craft_brew_dev --apply
Creates missing tank location metadata only; never creates quants or moves.
"""
import argparse

import odoo
from odoo import api, SUPERUSER_ID


def repair(env):
    from odoo.addons.craft_brew_api.services.master_data import MasterDataService

    tanks = env['brew.tank'].sudo().search([('active', '=', True), ('stock_location_id', '=', False)], order='company_id,factory_id,name')
    before = [(tank.id, tank.current_volume_l, tank.current_production_batch_id.id, tank.status) for tank in tanks]
    quant_count = env['stock.quant'].sudo().search_count([])
    move_count = env['stock.move'].sudo().search_count([])
    for tank in tanks:
        administrator = env.ref('craft_brew_core.group_brew_admin').users.filtered(lambda user: user.active and tank.company_id in user.company_ids)[:1]
        if not administrator:
            raise RuntimeError('No authorized brewery administrator for company '+str(tank.company_id.id))
        service = MasterDataService(env, administrator, tank.company_id)
        location = service._ensure_tank_location(tank)
        print(f'{tank.factory_id.name}: {tank.name} -> {location.name} (location {location.id})')
    assert before == [(tank.id, tank.current_volume_l, tank.current_production_batch_id.id, tank.status) for tank in tanks]
    assert quant_count == env['stock.quant'].sudo().search_count([])
    assert move_count == env['stock.move'].sudo().search_count([])
    return len(tanks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    odoo.tools.config.parse_config(['-c', '/etc/odoo/odoo.conf', '-d', args.database, '--no-http'])
    with odoo.registry(args.database).cursor() as cr:
        try:
            count = repair(api.Environment(cr, SUPERUSER_ID, {}))
            if args.apply:
                cr.commit()
            print(f'{"Applied" if args.apply else "Preview only"}: {count} tank location associations')
        finally:
            cr.rollback()
