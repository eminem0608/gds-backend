"""Odoo shell integration checks; fixture records are always rolled back."""
import json
import uuid
from datetime import timedelta
from odoo import Command, fields


def run(env):
    from odoo.addons.craft_brew_api.controllers.base import ApiError
    from odoo.addons.craft_brew_api.services.trace import TraceService
    from odoo.addons.craft_brew_api.services.product_trace import ProductTrace
    from odoo.addons.craft_brew_api.services.master_data import MasterDataService
    from odoo.addons.craft_brew_api.services.planning import PlanningService
    prefix = 'PT' + uuid.uuid4().hex[:7]
    company = env.company
    fa, fb = [env['brew.factory'].create({'name': prefix + suffix, 'code': prefix + suffix, 'company_id': company.id}) for suffix in ('A', 'B')]
    root = env['brew.organization'].create({'name': prefix + '总部', 'code': prefix + 'HQ', 'company_id': company.id, 'organization_type': 'company'})
    org = env['brew.organization'].create({'name': prefix + 'A', 'code': prefix + 'ORG', 'company_id': company.id, 'organization_type': 'factory', 'factory_id': fa.id, 'parent_id': root.id})

    def actor(suffix, role, organization):
        return env['res.users'].create({'name': prefix + suffix, 'login': prefix + suffix, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_organization_id': organization.id,
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})

    admin = actor('HQ', 'admin', root)
    local = actor('LOCAL', 'warehouse', org)
    packer = actor('PACKER', 'operator', org)
    product = env['product.template'].create({'name': prefix + '拉格', 'company_id': company.id, 'brew_item_type': 'finished_product', 'default_code': '00' + prefix + '-LAGER'})
    other_product = product.copy({'name': prefix + '艾尔'})
    master = MasterDataService(env, admin, company)
    code = '00' + prefix + '-500CAN'
    spec, serialized = master.create('packaging-specs', {'name': '500mL 罐装', 'product_id': product.product_variant_id.id,
        'barcode': code, 'qty': 1, 'brew_volume_l': 0.5, 'brew_barcode_level': 'can'})
    assert serialized['barcode'] == code and serialized['product_tmpl_id'] == str(product.id)
    assert master.serialize('products', product)['product_variant_id'] == str(product.product_variant_id.id)
    keg, _ = master.create('packaging-specs', {'name': '20L 桶装', 'product_id': product.product_variant_id.id,
        'barcode': prefix + '-20KEG', 'qty': 1, 'brew_volume_l': 20, 'brew_barcode_level': 'keg'})

    def rejects(call, expected):
        try:
            with env.cr.savepoint():
                call()
        except ApiError as cause:
            assert cause.code == expected, (cause.code, cause.message)
        else:
            raise AssertionError('Expected ' + expected)

    rejects(lambda: master.create('packaging-specs', {'name': '重复', 'product_id': other_product.product_variant_id.id,
        'barcode': code, 'qty': 1, 'brew_volume_l': 20, 'brew_barcode_level': 'keg'}), 'VALIDATION_DUPLICATE')
    equipment = env['maintenance.equipment'].create({'name': prefix, 'company_id': company.id, 'brew_factory_id': fa.id, 'brew_equipment_type': 'mash'})
    tank = env['brew.tank'].create({'name': prefix, 'factory_id': fa.id, 'capacity_l': 2000})
    other_equipment = equipment.copy({'brew_factory_id': fb.id})
    other_tank = tank.copy({'factory_id': fb.id})
    recipe = env['brew.recipe'].create({'name': prefix, 'code': prefix, 'company_id': company.id, 'product_tmpl_id': product.id, 'equipment_id': equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 200, 'state': 'active'})
    now = fields.Datetime.now()

    def batch(factory, package=None, packed=True):
        plan = env['brew.production.plan'].create({'company_id': company.id, 'factory_id': factory.id,
            'product_tmpl_id': product.id, 'recipe_version_id': version.id, 'planned_volume_l': 200,
            'planned_start': now - timedelta(days=20), 'planned_end': now - timedelta(days=1),
            'equipment_id': equipment.id if factory == fa else other_equipment.id,
            'tank_id': tank.id if factory == fa else other_tank.id, 'state': 'done', 'packaging_owner_id': packer.id,
            'brewing_completion_json': json.dumps({'completed_at': (now - timedelta(days=1)).isoformat(), 'actual_volume_l': 200, 'completed_by': packer.name})})
        if package:
            plan.packaging_plan_json = json.dumps({'materials_only': True, 'finished_packaging_id': str(package.id),
                **({'execution': {'completed_at': now.isoformat(), 'completed_by': packer.name}} if packed else {})})
        return env['brew.production.batch'].create({'production_plan_id': plan.id, 'state': 'completed',
            'actual_volume_l': 200, 'started_at': now - timedelta(days=10), 'completed_at': now - timedelta(days=1)})

    first = batch(fa, spec)
    second = batch(fb, spec)
    third = batch(fa, spec, False)
    unrelated = batch(fa, keg)
    unlinked = batch(fa)
    plan = first.production_plan_id
    task = env['brew.task'].create({'name': prefix + '投料', 'company_id': company.id, 'factory_id': fa.id,
        'task_type': 'mashing', 'target_model': plan._name, 'target_id': plan.id, 'state': 'done',
        'assignee_id': packer.id, 'started_at': now - timedelta(days=10), 'completed_at': now - timedelta(days=9),
        'execution_checkpoints_json': json.dumps([{'code': 'malt', 'title': '麦芽投料', 'state': 'done',
            'completed_at': (now - timedelta(days=10)).isoformat(), 'completed_by': packer.name, 'actual_quantity': 20, 'unit': 'kg'}])})
    trace = ProductTrace(TraceService(env, admin, company))
    result = trace.search(code, 1, 2)
    assert result['product']['code'] == code and result['total'] == 3 and result['has_more']
    all_ids = {r['id'] for r in result['batches'] + trace.search(code, 2, 2)['batches']}
    assert all_ids == {str(b.id) for b in first | second | third}
    assert str(unrelated.id) not in all_ids and str(unlinked.id) not in all_ids
    details = trace.detail(first.id, spec.id)
    feed = next(e for e in details['events'] if e['title'] == '麦芽投料')
    assert feed['by'] == packer.name and feed['at'] and feed['detail'] == '20 kg'
    assert details['batch']['packaged_by'] == packer.name and details['batch']['packaged_at'] == now.isoformat()
    assert trace.detail(third.id, spec.id)['batch']['packaged_at'] is None
    local_trace = ProductTrace(TraceService(env, local, company))
    assert {r['id'] for r in local_trace.search(code)['batches']} == {str(first.id), str(third.id)}
    rejects(lambda: local_trace.detail(second.id, spec.id), 'RESOURCE_NOT_FOUND')
    rejects(lambda: trace.detail(unrelated.id, spec.id), 'RESOURCE_NOT_FOUND')
    rejects(lambda: trace.search('UNKNOWN-' + prefix), 'RESOURCE_NOT_FOUND')
    rejects(lambda: trace.search(''), 'VALIDATION_REQUIRED')
    rejects(lambda: master.update('packaging-specs', spec.id, {'version': spec.write_date.isoformat(), 'barcode': prefix + 'CHANGED'}), 'STATE_PRECONDITION_FAILED')
    rejects(lambda: master.update('packaging-specs', spec.id, {'version': spec.write_date.isoformat(), 'brew_volume_l': 1}), 'STATE_PRECONDITION_FAILED')
    spec.active = False
    assert trace.search(code)['total'] == 3, 'Archived identifiers must retain historical traceability'
    spec.active = True
    # A newly scheduled material-only task stores the fixed finished-product relationship.
    material = env['product.template'].create({'name': prefix + '包装罐', 'company_id': company.id, 'brew_item_type': 'packaging_material',
        'brew_packaging_type': 'can', 'brew_packaging_state': 'confirmed', 'brew_packaging_details': {'capacity': 500, 'capacity_unit': 'mL'}})
    packaging = {'materials_only': True, 'materials': [{'material_id': material.id, 'quantity': 10}],
        'planned_start': (now + timedelta(hours=1)).isoformat(), 'planned_end': (now + timedelta(hours=2)).isoformat()}
    planning = PlanningService(env, admin, company)
    payload = {'packaging_owner_id': packer.id, 'packaging_plan': packaging}
    assert json.loads(planning._packaging_values(payload, fa, product, 200, now)['packaging_plan_json'])['materials_only']
    packaging['finished_packaging_id'] = spec.id
    values = planning._packaging_values(payload, fa, product, 200, now)
    assert json.loads(values['packaging_plan_json'])['packaging_barcode'] == code
    foreign = env['product.packaging'].create({'name': prefix, 'company_id': company.id, 'product_id': other_product.product_variant_id.id, 'barcode': prefix + 'OTHER', 'brew_volume_l': 0.5})
    packaging['finished_packaging_id'] = foreign.id
    rejects(lambda: planning._packaging_values(payload, fa, product, 200, now), 'VALIDATION_RELATION')
    # The finished identifier exists before packaging and survives later product edits.
    product_code = product.default_code
    original_code = unlinked.finished_product_code
    assert original_code == product_code + '-' + unlinked.name
    assert first.finished_product_code != second.finished_product_code
    exact = trace.search(original_code)
    assert exact['total'] == 1 and exact['packaging_id'] is None
    assert exact['batches'][0]['id'] == str(unlinked.id)
    assert trace.detail(unlinked.id)['batch']['finished_product_code'] == original_code
    assert not trace.detail(unlinked.id)['batch']['packaged_at']
    assert trace.search(product_code)['total'] == 5
    assert local_trace.search(product_code)['total'] == 4
    rejects(lambda: local_trace.search(second.finished_product_code), 'RESOURCE_NOT_FOUND')
    product.default_code = prefix + '-NEW'
    assert unlinked.finished_product_code == original_code
    assert trace.search(original_code)['batches'][0]['id'] == str(unlinked.id)
    assert trace.search(product_code)['total'] == 5
    from odoo.addons.craft_brew_api.services.stock_browser import StockBrowserService
    stock = StockBrowserService(env, admin, company)
    raw = stock.list({'category': 'semi_finished', 'keyword': original_code}, 1, 30)
    assert raw['total'] == 1 and raw['items'][0]['finished_product_code'] == original_code
    assert str(raw['items'][0]['batch_id']) == str(unlinked.id)
    assert not StockBrowserService(env, local, company).list({'category': 'semi_finished', 'keyword': second.finished_product_code}, 1, 30)['total']
    # A packaging specification no longer needs a second mandatory identifier.
    _, no_barcode = master.create('packaging-specs', {'name': '330mL 瓶装', 'product_id': product.product_variant_id.id,
        'qty': 1, 'brew_volume_l': 0.33, 'brew_barcode_level': 'bottle'})
    assert not no_barcode['barcode']
    # Finished inventory links each physical lot to its originating batch code.
    location = env['stock.location'].create({'name': prefix + '成品库', 'usage': 'internal',
        'company_id': company.id, 'brew_factory_id': fa.id, 'brew_location_type': 'finished'})
    product.write({'type': 'product', 'tracking': 'lot'})
    for source, qty in [(third, 10), (unlinked, 20)]:
        session = env['brew.fermentation.session'].create({'batch_id': source.id, 'source_tank_id': tank.id,
            'tank_id': tank.id, 'state': 'ready', 'current_volume_l': 200})
        lot = env['stock.lot'].create({'name': prefix + '-' + str(qty), 'product_id': product.product_variant_id.id,
            'company_id': company.id, 'brew_quality_status': 'qualified'})
        env['stock.quant']._update_available_quantity(product.product_variant_id, location, qty, lot_id=lot)
        env['brew.packaging.order'].create({'session_id': session.id, 'product_id': product.product_variant_id.id,
            'packaging_id': spec.id, 'finished_location_id': location.id, 'planned_volume_l': 200,
            'state': 'released', 'output_qty': qty, 'output_volume_l': qty * 0.5, 'finished_lot_id': lot.id})
    finished = stock.list({'category': 'finished', 'factory_id': fa.id}, 1, 30)
    row = next(r for r in finished['items'] if r['product']['id'] == str(product.product_variant_id.id))
    assert row['on_hand'] == 30 and set(row['finished_product_codes']) == {third.finished_product_code, original_code}
    filtered = stock.list({'category': 'finished', 'keyword': original_code}, 1, 30)
    assert filtered['total'] == 1 and filtered['items'][0]['on_hand'] == 20
    assert filtered['items'][0]['finished_product_codes'] == [original_code]
    lot_details = stock.detail(product.product_variant_id.id, fa.id)['lots']
    assert {lot['finished_product_code'] for lot in lot_details} == {third.finished_product_code, original_code}
    assert trace.search(original_code)['batches'][0]['id'] == str(unlinked.id)
    print('PASS: automatic batch finished identifiers, raw-liquid trace without packaging, product-code lookup, stable snapshots, inventory-code search, factory isolation, optional packaging barcode, finished-lot lineage and filtered quantities, and legacy packaging trace')



if 'env' in globals():
    try:
        run(env)
    finally:
        env.cr.rollback()
else:
    # Also runnable as a concrete, noninteractive script in the dev container.
    import odoo
    from odoo import api, SUPERUSER_ID
    odoo.tools.config.parse_config(['-c', '/etc/odoo/odoo.conf', '-d', 'craft_brew_dev', '--no-http'])
    odoo.service.server.load_server_wide_modules()
    with odoo.registry('craft_brew_dev').cursor() as cursor:
        test_env = api.Environment(cursor, SUPERUSER_ID, {})
        try:
            run(test_env)
        finally:
            cursor.rollback()
