"""Run inside `odoo shell`; the entire check rolls back its test records."""
from odoo import Command
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.packaging import PackagingService
from odoo.addons.craft_brew_api.services.stock_browser import StockBrowserService
from odoo.addons.craft_brew_api.services.master_data import MasterDataService
from odoo.addons.craft_brew_api.services.purchase import PurchaseService


def rejects(call, status):
    try:
        call()
    except ApiError as exc:
        assert exc.status == status, (exc.code, exc.status)
    else:
        raise AssertionError('Expected rejection')


try:
    admin = env.ref('base.user_admin')
    company = admin.company_id
    supplier = env['res.users'].search([('login', '=', 'gys2')], limit=1)
    other = env['res.users'].search([('login', '=', 'gys')], limit=1)
    own = PackagingService(env, supplier, company)
    manage = PackagingService(env, admin, company)
    category = next(c for c in own.schema()['categories'] if c['type'] == 'bottle')
    values = {'name': 'TEST packaging bottle', 'type': 'bottle', 'category_code': category['code'],
              'details': {'capacity': 330, 'capacity_unit': 'mL', 'material': '玻璃', 'closure': '26mm',
                          'unit': '个', 'procurement_unit': '箱', 'unit_quantity': 24, 'quote_price': 48}}
    record, data = own.save(values)
    assert record.brew_supplier_id == supplier.brew_supplier_id
    assert record.brew_supplier_price == 2
    rejects(lambda: PackagingService(env, other, company).record(record.id), 404)
    rejects(lambda: own.action(record.id, 'confirm', {'version': record.write_date.isoformat()}), 403)
    rejects(lambda: PurchaseService(env, admin, company)._product(record.product_variant_id.id), 422)
    assert record.id not in env['product.template'].search(MasterDataService(env, admin, company)._domain('materials')).ids
    own.action(record.id, 'submit', {'version': record.write_date.isoformat()})
    manage.action(record.id, 'return', {'version': record.write_date.isoformat(), 'note': '补充规格'})
    assert record.brew_packaging_state == 'returned'
    own.action(record.id, 'submit', {'version': record.write_date.isoformat()})
    manage.action(record.id, 'confirm', {'version': record.write_date.isoformat()})
    assert PurchaseService(env, admin, company)._product(record.product_variant_id.id)
    own.save({**values, 'version': record.write_date.isoformat()}, record.id)
    assert record.brew_packaging_state == 'draft'
    own.action(record.id, 'archive', {'version': record.write_date.isoformat()})
    assert not record.active
    own.action(record.id, 'restore', {'version': record.write_date.isoformat()})
    assert record.active
    print('PASS supplier isolation, conversion, review, purchase guard, edit review reset, archive/restore')

    browse = StockBrowserService(env, admin, company)
    rows = browse.list({}, 1, 100)
    for row in rows['items']:
        detail = browse.detail(row['product']['id'], row['factory']['id'])
        assert abs(detail['available'] - row['available']) < 1e-6
        assert abs(detail['on_hand'] - row['on_hand']) < 1e-6
    factories = env['brew.factory'].search([('company_id', '=', company.id)], limit=2)
    assert len(factories) == 2
    user = env['res.users'].with_context(no_reset_password=True).create({
        'name': 'TEST inventory scope', 'login': 'test-inventory-browser-scope',
        'company_id': company.id, 'company_ids': [Command.set([company.id])],
        'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_warehouse').id])],
        'brew_factory_ids': [Command.set(factories[:1].ids)]})
    scoped = StockBrowserService(env, user, company)
    assert all(r['factory']['id'] == str(factories[0].id) for r in scoped.list({}, 1, 100)['items'])
    rejects(lambda: scoped.list({'factory_id': factories[1].id}, 1, 30), 403)
    plan = env['brew.production.plan'].browse(9)
    product = env['product.product'].with_context(active_test=False).search([('product_tmpl_id', '=', plan.product_tmpl_id.id)], limit=1)
    if not product:
        product = env['product.product'].create({'product_tmpl_id': plan.product_tmpl_id.id})
    history = browse.detail(product.id, plan.factory_id.id)
    assert any(b['plan'] == plan.name for b in history['production_history'])
    rejects(lambda: scoped.detail(product.id, factories[1].id), 403)
    print('PASS stock balances, brewery permissions, and historical production provenance')
finally:
    env.cr.rollback()
