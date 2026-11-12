"""Admin enters the supplier form; all isolated fixtures are rolled back."""
import uuid
from odoo import Command
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.master_data import MasterDataService
from odoo.addons.craft_brew_api.services.packaging import PackagingService


def run(env):
    prefix = 'SC' + uuid.uuid4().hex[:8]
    company = env.company
    admin = env.ref('base.user_admin')
    suppliers = env['res.partner'].create([
        {'name': prefix + suffix, 'company_id': company.id, 'supplier_rank': 1}
        for suffix in ('A', 'B')])
    other_company = env['res.company'].create({'name': prefix + 'Other'})
    foreign = env['res.partner'].create({'name': prefix + 'Foreign', 'company_id': other_company.id, 'supplier_rank': 1})
    def actor(suffix, role, supplier=None):
        return env['res.users'].create({'name': prefix + suffix, 'login': prefix + suffix,
            'company_id': company.id, 'company_ids': [Command.set([company.id])],
            'brew_supplier_id': supplier.id if supplier else False,
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})
    owner = actor('Owner', 'supplier', suppliers[0])
    other = actor('Other', 'supplier', suppliers[1])
    viewer = actor('Viewer', 'operator')
    purchase = actor('Purchase', 'purchase')
    def rejects(call, code):
        try:
            with env.cr.savepoint():
                call()
        except ApiError as error:
            assert error.code == code, (error.code, error.message)
        else:
            raise AssertionError('Expected ' + code)
    master = MasterDataService(env, admin, company)
    category = env['brew.material.category'].search([('material_group', '=', 'malt'), ('parent_id', '!=', False), ('active', '=', True)], limit=1)
    assert category
    raw = {'name': prefix, 'brew_item_type': 'raw_material', 'brew_material_category': category.code,
        'brew_brand': 'Test', 'brew_specification': '25kg', 'brew_extract_rate': 80, 'brew_color_ebc': 4,
        'brew_supplier_price': 12, 'brew_supplier_quantity': 100, 'weight_uom': 'kg', 'brew_supplier_id': suppliers[0].id}
    product, data = master.create('materials', raw)
    assert data['supplier']['id'] == str(suppliers[0].id)
    own_master, other_master = MasterDataService(env, owner, company), MasterDataService(env, other, company)
    assert own_master.get('materials', product.id)['id'] == str(product.id)
    rejects(lambda: other_master.get('materials', product.id), 'RESOURCE_NOT_FOUND')
    master.update('materials', product.id, {**raw, 'brew_supplier_price': 13, 'version': product.write_date.isoformat()})
    assert own_master.get('materials', product.id)['brew_supplier_price'] == 13
    rejects(lambda: master.create('materials', {**raw, 'brew_supplier_id': foreign.id}), 'DATA_SCOPE_DENIED')
    rejects(lambda: master.create('materials', {**raw, 'brew_brand': ''}), 'VALIDATION_REQUIRED')
    rejects(lambda: MasterDataService(env, viewer, company).create('materials', raw), 'PERMISSION_DENIED')
    # Supplier accounts always write to their own supplier, including forged requests.
    own_product, _ = own_master.create('materials', {**raw, 'brew_supplier_id': suppliers[1].id})
    assert own_product.brew_supplier_id == suppliers[0]
    manager = PackagingService(env, admin, company)
    schema = manager.schema()
    bottle_category = next(item['code'] for item in schema['categories'] if item['type'] == 'bottle')
    payload = {'name': prefix + 'Bottle', 'code': prefix, 'type': 'bottle', 'category_code': bottle_category,
        'supplier_catalog': True, 'supplier_id': suppliers[0].id,
        'details': {'unit': '个', 'procurement_unit': '个', 'unit_quantity': 1, 'quote_price': 2,
            'capacity': 500, 'capacity_unit': 'mL', 'material': '玻璃'}}
    bottle, data = manager.save(payload)
    assert bottle.brew_supplier_id == suppliers[0] and bottle.brew_packaging_catalog_owner_id == suppliers[0]
    own = PackagingService(env, owner, company)
    assert own.record(bottle.id) == bottle
    rejects(lambda: PackagingService(env, other, company).record(bottle.id), 'RESOURCE_NOT_FOUND')
    assert all(row['id'] != str(bottle.id) for row in manager.list(1, 100)['items'])
    assert any(row['id'] == str(bottle.id) for row in manager.list(1, 100, keyword=prefix, catalog=True)['items'])
    manager.save({**payload, 'name': prefix + 'Updated', 'version': bottle.write_date.isoformat()}, bottle.id)
    assert own.record(bottle.id).name.endswith('Updated')
    rejects(lambda: manager.save({**payload, 'supplier_id': None}), 'VALIDATION_REQUIRED')
    rejects(lambda: manager.save({**payload, 'supplier_id': foreign.id}), 'DATA_SCOPE_DENIED')
    rejects(lambda: PackagingService(env, purchase, company).save(payload), 'PERMISSION_DENIED')
    rejects(lambda: PackagingService(env, other, company).save(payload), 'PERMISSION_DENIED')
    manager.action(bottle.id, 'archive', {'supplier_catalog': True, 'version': bottle.write_date.isoformat()})
    assert not bottle.active
    manager.action(bottle.id, 'restore', {'supplier_catalog': True, 'version': bottle.write_date.isoformat()})
    assert bottle.active and bottle.brew_packaging_catalog_owner_id == suppliers[0]
    print('PASS admin supplier raw/packaging create/edit/archive/restore; supplier form validation; configured supplier ownership; company and account isolation; supplier cannot impersonate another supplier')


try:
    run(env)
finally:
    env.cr.rollback()
