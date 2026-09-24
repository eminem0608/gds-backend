"""Execute with odoo shell in the development DB. All fixtures are rolled back."""
import copy
import json
from odoo.addons.craft_brew_api.services.recipe import RecipeService
from odoo.addons.craft_brew_api.services.master_data import MasterDataService
from odoo.addons.craft_brew_api.controllers.base import ApiError

try:
    user = env.ref('base.user_admin')
    company = user.company_id
    service = RecipeService(env, user, company)
    supplier = env['res.partner'].create({'name': 'ROLLBACK Recipe Supplier', 'company_id': company.id, 'supplier_rank': 1})
    kg = env.ref('uom.product_uom_kgm')
    materials = {}
    for group in ['malt', 'hop', 'yeast']:
        materials[group] = env['product.template'].create({'name': 'ROLLBACK ' + group, 'company_id': company.id, 'brew_item_type': 'raw_material', 'brew_material_category': group, 'brew_supplier_id': supplier.id, 'uom_id': kg.id, 'uom_po_id': kg.id, 'standard_price': 10})
    equipment = env['maintenance.equipment'].create({'name':'ROLLBACK 20L', 'company_id':company.id, 'brew_equipment_type':'mash', 'brew_capacity_l':20})
    parent = env['brew.beer.category'].search([('parent_id','=',False)], limit=1)
    child = env['brew.beer.category'].create({'name':'ROLLBACK Style', 'code':'rollback_recipe_test_style', 'parent_id':parent.id})
    rules = [dict(condition='time', schedule_type=s, day_value=4, interval_days=2, action='measure_gravity', target='') for s in ['daily','day','interval']]
    rules += [dict(condition='temperature', operator='<=', condition_value=18, action='cool_to', target='4')]
    design = dict(schema_version=2, brew_volume_l=20, brew_equipment_id=equipment.id, ibu_method='tinseth', targets={}, attenuation_override='',
        fermentables=[dict(material_id=materials['malt'].id, quantity=4, extract_rate=80, color=4)],
        hops=[dict(material_id=materials['hop'].id, quantity=20, alpha_acid=10, use='boil', time=60)],
        yeast=[dict(material_id=materials['yeast'].id, quantity=11, attenuation=75, temperature=18, flavor='clean')],
        mash=dict(efficiency=72, water_grain_ratio=3.5, absorption_rate=.8, boil_time_min=60, evaporation_rate=10, screen_water_l=0, sparge_count=2, sparge_mode='manual', sparge_water_liters=[5.6,5.6], steps=[dict(name='Saccharification',temperature=65,duration_min=60)]),
        fermentation=dict(target_temperature=18,target_days=10,cold_storage_temperature=2,steps=rules))
    recipe, response = service.create_recipe(dict(name='ROLLBACK TEST', beer_category_id=parent.id, beer_subcategory_id=child.id, initial_version=dict(base_volume_l=20,design=design)))
    data = response['initial_version']
    assert data['design'] == design
    assert len(data['materials']) == 3
    assert [m['uom_id'] for m in data['materials']] == [str(kg.id), str(env.ref('uom.product_uom_gram').id), str(env.ref('uom.product_uom_gram').id)]
    saved_rules = [s['config']['fermentation_rule'] for s in data['process_steps'] if 'fermentation_rule' in s['config']]
    assert saved_rules == rules
    required = {s['action_type'] for s in data['process_steps'] if s['mandatory_confirmation']}
    assert required == {'enter_tank','transfer','packaging_release'}
    record, data = service.update_version(data['id'], dict(record_version=data['record_version'], base_volume_l=20, design=design))
    assert len(data['materials']) == 3, 'Repeated saves must replace rather than append'
    reread = service.serialize_version(service._version(data['id']), True)
    assert reread['design'] == design
    assert abs(record.calculate(20)['estimated_cost'] - 40.31) < .01
    before = env['brew.recipe'].search_count([])
    invalid_design = copy.deepcopy(design)
    invalid_design['brew_volume_l'] = 50
    try:
        with env.cr.savepoint():
            service.create_recipe(dict(name='ROLLBACK INVALID', beer_category_id=parent.id, beer_subcategory_id=child.id, initial_version=dict(base_volume_l=20,design=invalid_design)))
    except ApiError:
        pass
    else:
        raise AssertionError('Invalid initial version accepted')
    assert env['brew.recipe'].search_count([]) == before, 'Failed initial version must not leave an orphan recipe'
    for mutate in [lambda d: d['hops'][0].update(quantity=-1), lambda d: d['fermentation']['steps'][1].update(day_value=0), lambda d: d['mash'].update(sparge_water_liters=[1,1]), lambda d: d['hops'][0].update(material_id=materials['yeast'].id)]:
        bad = copy.deepcopy(design); mutate(bad)
        try:
            with env.cr.savepoint():
                service.update_version(data['id'],dict(record_version=data['record_version'],base_volume_l=20,design=bad))
        except ApiError:
            pass
        else:
            raise AssertionError('Invalid design accepted')
    try:
        service.update_version(data['id'],dict(record_version='stale',design=design))
    except ApiError as error:
        assert error.code == 'VERSION_CONFLICT'
    else:
        raise AssertionError('Stale update accepted')
    service.action(data['id'],'submit',dict(record_version=data['record_version']))
    assert record.state == 'submitted'
    try:
        with env.cr.savepoint():
            service.update_version(record.id,dict(record_version=record.write_date.isoformat(),design=design))
    except Exception:
        pass
    else:
        raise AssertionError('Submitted version allowed editing')
    service.action(record.id,'approve',dict(record_version=record.write_date.isoformat()))
    service.action(record.id,'activate',dict(record_version=record.write_date.isoformat()))
    assert record.state == 'active'
    copied, copy_data = service.copy_version(record.id)
    assert copied.state == 'draft' and copy_data['design'] == design and len(copy_data['materials']) == 3
    # Regression: duplicate generated code without a bound finished product.
    other, _ = service.create_recipe(dict(name='ROLLBACK TEST', beer_category_id=parent.id, beer_subcategory_id=child.id))
    assert other.code != recipe.code
    # New supplier catalogue records must no longer silently use Units.
    master = MasterDataService(env,user,company)
    values = master._values('materials',dict(name='test',brew_material_category='malt'),create=True)
    assert values['uom_id'] == kg.id and values['uom_po_id'] == kg.id
    print('PASS: save/reload, units/cost, rules, replace, validation, conflict, submit/approve/activate/copy, duplicate codes, catalogue defaults')
finally:
    env.cr.rollback()
    print('ROLLBACK: no test records committed')
