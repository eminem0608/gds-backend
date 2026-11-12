"""Run in Odoo shell; all fixtures and writes are rolled back."""
import json
import uuid
from datetime import timedelta

from odoo import fields
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.recipe_design import design_values, mash_water_requirements
from odoo.addons.craft_brew_api.services.planning import PlanningService


def run(env):
    prefix = 'MANUAL-MASH-' + uuid.uuid4().hex[:8]
    company = env.company
    factory = env['brew.factory'].create({'name': prefix, 'code': prefix, 'company_id': company.id})
    equipment = env['maintenance.equipment'].create({'name': prefix, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_equipment_type': 'mash'})
    tank = env['brew.tank'].create({'name': prefix, 'factory_id': factory.id, 'capacity_l': 2000})
    product = env['product.template'].create({'name': prefix, 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'finished_product'})
    recipe = env['brew.recipe'].create({'name': prefix, 'code': prefix, 'company_id': company.id, 'product_tmpl_id': product.id, 'equipment_id': equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 20, 'state': 'active'})
    design = {
        'schema_version': 2, 'brew_volume_l': 20, 'brew_equipment_id': equipment.id, 'ibu_method': 'tinseth',
        'fermentables': [], 'hops': [], 'yeast': [], 'fermentation': {},
        'mash': {
            'efficiency': 72, 'water_grain_ratio': 3.5, 'absorption_rate': .8,
            'boil_time_min': 60, 'evaporation_rate': 10, 'screen_water_l': 0,
            'sparge_count': 3, 'sparge_water_liters': [7.33, 7.33, 7.34],
            'steps': [{'name': '糖化', 'temperature': 65, 'duration_min': 30}, {'name': '升温', 'temperature': 72, 'duration_min': 10}],
            'workflow': {'execution_mode': 'manual', 'operations': {'manual': [
                {'id': 'custom-check', 'name': '检查过滤结果', 'input_mode': 'choice', 'target': '清澈 / 浑浊'},
                {'id': 'custom-volume', 'name': '记录冷却体积', 'input_mode': 'number', 'target': 'L'},
            ]}},
        },
    }
    start = fields.Datetime.now()
    plan = env['brew.production.plan'].create({
        'name': prefix, 'company_id': company.id, 'factory_id': factory.id,
        'product_tmpl_id': product.id, 'recipe_version_id': version.id, 'planned_volume_l': 20,
        'planned_start': start, 'planned_end': start + timedelta(days=2),
        'equipment_id': equipment.id, 'tank_id': tank.id,
        'recipe_snapshot_json': json.dumps({'design': design}),
    })
    task = env['brew.task'].create({'name': prefix, 'task_type': 'mashing', 'company_id': company.id, 'factory_id': factory.id, 'target_model': plan._name, 'target_id': plan.id})
    nodes = task.mash_execution_checkpoints()
    assert [row['code'] for row in nodes] == ['rest_0_start', 'rest_0_end', 'rest_1_start', 'rest_1_end', 'custom-check', 'custom-volume']
    assert nodes[1]['timer_from'] == 'rest_0_start' and nodes[1]['due_after_min'] == 30
    assert nodes[-2]['choices'] == ['清澈', '浑浊'] and nodes[-1]['input_mode'] == 'number'
    assert nodes[-1]['unit'] == 'L'
    assert task.mash_execution_checkpoints() == nodes
    canonical = design_values(env, company, design, 20)
    names = [command[2]['name'] for command in canonical['process_step_ids'] if command[0] == 0]
    assert names[:4] == ['糖化', '升温', '检查过滤结果', '记录冷却体积'] and '煮沸' not in names
    for mode in ['invalid', 'choice']:
        bad = json.loads(json.dumps(design))
        bad['mash']['workflow']['operations']['manual'][0].update(input_mode=mode, target='')
        try:
            design_values(env, company, bad, 20)
        except ApiError as exc:
            assert exc.code == 'VALIDATION_FORMAT'
        else:
            raise AssertionError('Invalid operation was accepted')
    design['mash']['workflow']['operations']['manual'].reverse()
    plan.write({'recipe_snapshot_json': json.dumps({'design': design})})
    task.write({'execution_checkpoints_json': '[]'})
    assert [node['code'] for node in task.mash_execution_checkpoints()][-2:] == ['custom-volume', 'custom-check']
    design['mash']['steps'] = []
    design['mash']['workflow']['operations']['manual'] = []
    plan.write({'recipe_snapshot_json': json.dumps({'design': design})})
    task.write({'execution_checkpoints_json': '[]'})
    assert task.mash_execution_checkpoints() == []
    # Existing snapshots retain the legacy execution path.
    design['mash']['workflow'] = {}
    plan.write({'recipe_snapshot_json': json.dumps({'design': design})})
    task.write({'execution_checkpoints_json': '[]'})
    assert any(row['code'] == 'boil_start' for row in task.mash_execution_checkpoints())
    # The original design configures temperatures and sparge water; execution
    # still supplies the standard material, filtering, boiling and transfer flow.
    design['mash'].update(water_calculation='original', boil_time_min=90)
    design['mash']['steps'] = [{'name': '糖化', 'temperature': 65, 'duration_min': 30}]
    design['mash']['workflow'] = {'execution_mode': 'standard', 'operations': {'manual': [{'id': 'stale', 'name': '不应执行'}]}}
    plan.write({'recipe_snapshot_json': json.dumps({'design': design})})
    task.write({'execution_checkpoints_json': '[]'})
    nodes = task.mash_execution_checkpoints()
    codes = [row['code'] for row in nodes]
    assert all(code in codes for code in ['confirm_materials', 'mash_in', 'rest_0_start', 'rest_0_end', 'filter_start', 'boil_start', 'transfer_fermenter'])
    assert 'stale' not in codes
    assert [row['quantity'] for row in nodes if row['code'].startswith('sparge_')] == [7.33, 7.33, 7.34]
    design_values(env, company, design, 20)
    bad = json.loads(json.dumps(design))
    bad['mash']['sparge_water_liters'][0] += .01
    try:
        design_values(env, company, bad, 20)
    except ApiError as exc:
        assert exc.code == 'VALIDATION_FORMAT'
    else:
        raise AssertionError('Mismatched sparge allocation was accepted')
    mash = {'water_calculation': 'original', 'evaporation_rate': 10, 'boil_time_min': 90, 'water_grain_ratio': 3, 'absorption_rate': .8, 'screen_water_l': 30, 'sparge_count': 3}
    pre_volume, sparge = mash_water_requirements(mash, 500, 100)
    assert abs(pre_volume - 550) < 1e-8 and abs(sparge - 250) < 1e-8
    source = {'brew_volume_l': 500, 'fermentables': [{'quantity': 100}], 'mash': mash}
    scaled = PlanningService._scale_production_design(None, source, 1000)
    assert scaled['mash']['sparge_water_liters'] == [166.66, 166.66, 166.68]
    print('PASS: manual-only execution, temperature timers, choice/number records, saved process steps, validation, order, empty workflow, legacy snapshots')
    print('PASS: original design formulas, standard execution, sparge allocations, mismatch rejection, production scaling')


try:
    run(env)
finally:
    env.cr.rollback()
