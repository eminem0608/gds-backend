"""Run in Odoo shell; all fixtures and execution changes are rolled back."""
import json
import uuid
from datetime import timedelta
from odoo import Command, fields
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.planning import PlanningService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService


def run(env):
    prefix = 'PACK-PLAN-' + uuid.uuid4().hex[:8]
    company = env.company
    factory = env['brew.factory'].create({'name': prefix, 'code': prefix, 'company_id': company.id})
    equipment = env['maintenance.equipment'].create({'name': prefix, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_equipment_type': 'mash'})
    tank = env['brew.tank'].create({'name': prefix, 'factory_id': factory.id, 'capacity_l': 2000, 'cleaning_status': 'clean'})
    product = env['product.template'].create({'name': prefix, 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'finished_product'})
    recipe = env['brew.recipe'].create({'name': prefix, 'code': prefix, 'company_id': company.id, 'product_tmpl_id': product.id, 'equipment_id': equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 100, 'state': 'active'})
    def user(role, suffix):
        return env['res.users'].create({'name': prefix + suffix, 'login': prefix + suffix, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])],
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})
    director, brewer, packer = user('production_director', '-D'), user('operator', '-B'), user('operator', '-P')
    package = env['product.packaging'].create({'name': prefix + '-500mL', 'product_id': product.product_variant_id.id, 'qty': 1, 'brew_volume_l': 0.5, 'brew_barcode_level': 'bottle', 'company_id': company.id})
    material = env['product.template'].create({'name': prefix + '-500mL-bottle', 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'packaging_material', 'brew_packaging_type': 'bottle', 'brew_packaging_state': 'confirmed', 'brew_packaging_details': {'capacity': 500, 'capacity_unit': 'mL', 'custom': True, 'beer_product_id': str(product.id), 'unit': '个'}})
    start = fields.Datetime.now() + timedelta(days=1)
    packaging = {'material_id': material.id, 'packaging_method': 'box', 'units_per_box': 1, 'planned_qty': 180, 'planned_start': (start + timedelta(days=4)).isoformat(), 'planned_end': (start + timedelta(days=5)).isoformat(), 'notes': 'label check'}
    payload = {'factory_id': factory.id, 'recipe_version_id': version.id, 'equipment_id': equipment.id, 'tank_id': tank.id,
        'planned_volume_l': 100, 'planned_start': start.isoformat(), 'planned_end': (start + timedelta(days=3)).isoformat(),
        'mash_owner_id': brewer.id, 'fermentation_owner_id': brewer.id, 'packaging_owner_id': packer.id, 'packaging_plan': packaging}
    service = PlanningService(env, director, company)
    def rejected(call, code):
        try:
            with env.cr.savepoint():
                call()
        except ApiError as exc:
            assert exc.code == code, (exc.code, exc.message)
        else:
            raise AssertionError('Invalid action accepted')
    rejected(lambda: service.create({**payload, 'packaging_owner_id': None}), 'VALIDATION_REQUIRED')
    rejected(lambda: service.create({**payload, 'packaging_plan': {**packaging, 'planned_qty': 201}}), 'VALIDATION_RANGE')
    rejected(lambda: service.create({**payload, 'packaging_plan': {**packaging, 'planned_qty': 0.5}}), 'VALIDATION_RANGE')
    rejected(lambda: service.create({**payload, 'packaging_plan': {**packaging, 'planned_start': start.isoformat()}}), 'VALIDATION_RANGE')
    plan, data = service.create(payload)
    assert data['packaging_owner']['id'] == str(packer.id) and data['packaging_plan']['planned_volume_l'] == 90
    task = env['brew.task'].search([('target_model', '=', plan._name), ('target_id', '=', plan.id), ('task_type', '=', 'packaging')])
    assert len(task) == 1 and task.assignee_id == packer and task.state == 'pending'
    assert any(item['id'] == str(material.id) and item['volume_l'] == 0.5 for item in service.packaging_options(product.id))
    from types import SimpleNamespace
    requirement = SimpleNamespace(packaging_method='box', package_container_type='bottle', package_unit_volume_l=0.5, package_units_per_box=12)
    box_values = service._packaging_values({**payload, 'packaging_plan': {**packaging, 'planned_qty': 10, 'units_per_box': 12}}, factory, product, 100, start + timedelta(days=3), requirement)
    box_snapshot = json.loads(box_values['packaging_plan_json'])
    assert box_snapshot['planned_volume_l'] == 60 and box_snapshot['materials'][0]['quantity'] == 120
    rejected(lambda: service._packaging_values(payload, factory, product, 100, start + timedelta(days=3), SimpleNamespace(packaging_method='keg', package_container_type=False, package_unit_volume_l=10, package_units_per_box=0)), 'VALIDATION_RELATION')
    # Saved snapshots can be revalidated without duplicating the primary material.
    service._packaging_values({**payload, 'packaging_plan': box_snapshot}, factory, product, 100, start + timedelta(days=3), requirement)
    assert PlanningService(env, packer, company).progress(plan.id)['plan']['id'] == str(plan.id)
    assert any(item['id'] == str(plan.id) for item in PlanningService(env, packer, company).list(1, 100)[0])
    packer_workflow = WorkflowService(env, packer, company)
    rejected(lambda: packer_workflow.task_action(task.id, 'packaging-start', {'version': task.write_date.isoformat()}), 'STATE_PRECONDITION_FAILED')
    mash_task = env['brew.task'].search([('target_model', '=', plan._name), ('target_id', '=', plan.id), ('task_type', '=', 'mashing')], limit=1)
    rejected(lambda: WorkflowService(env, director, company).task_action(mash_task.id, 'start', {'version': mash_task.write_date.isoformat()}), 'PERMISSION_DENIED')
    rejected(lambda: PlanningService(env, packer, company).action(plan.id, 'set-packaging-plan', {**payload, 'version': plan.write_date.isoformat()}), 'PERMISSION_DENIED')
    plan.with_context(brew_state_transition=True).write({'state': 'done', 'brewing_completion_json': json.dumps({'actual_volume_l': 90})})
    batch = env['brew.production.batch'].search([('production_plan_id','=',plan.id)],limit=1)
    if not batch:
        batch = env['brew.production.batch'].create({'production_plan_id':plan.id,'state':'completed','actual_volume_l':90})
    else:
        batch.write({'actual_volume_l':90,'state':'completed'})
    source = env['stock.location'].create({'name':prefix+'材料库','usage':'internal','company_id':company.id,'brew_factory_id':factory.id,'brew_location_type':'packaging'})
    target = env['stock.location'].create({'name':prefix+'成品库','usage':'internal','company_id':company.id,'brew_factory_id':factory.id,'brew_location_type':'finished'})
    lot = env['stock.lot'].create({'name':prefix,'product_id':material.product_variant_id.id,'company_id':company.id,'brew_quality_status':'qualified'})
    env['stock.quant']._update_available_quantity(material.product_variant_id,source,2000,lot_id=lot)
    service.action(plan.id,'confirm-raw-location',{'version':plan.write_date.isoformat(),'tank_id':tank.id,'confirmed':True})
    assert not packer_workflow.serialize_task(task)['execution_blocked_reason']
    rejected(lambda: WorkflowService(env, brewer, company).task_action(task.id, 'packaging-start', {'version': task.write_date.isoformat()}), 'PERMISSION_DENIED')
    packer_workflow.task_action(task.id, 'packaging-start', {'version': task.write_date.isoformat()})
    assert task.state == 'in_progress'
    rejected(lambda: service.action(plan.id, 'set-packaging-plan', {**payload, 'version': plan.write_date.isoformat()}), 'STATE_PRECONDITION_FAILED')
    rejected(lambda: packer_workflow.task_action(task.id, 'packaging-complete', {'version': task.write_date.isoformat(), 'actual_qty': 181, 'loss_volume_l': 0, 'finished_packaging_id':package.id, 'finished_location_id':target.id, 'confirmed': True}), 'VALIDATION_RANGE')
    packer_workflow.task_action(task.id, 'packaging-complete', {'version': task.write_date.isoformat(), 'actual_qty': 178, 'loss_volume_l': 1, 'finished_packaging_id':package.id, 'finished_location_id':target.id, 'confirmed': True})
    result = service.serialize(plan)['packaging_plan']['execution']
    assert task.state == 'done' and result['actual_qty'] == 178 and result['completed_by_id'] == str(packer.id)
    rejected(lambda: packer_workflow.task_action(task.id, 'packaging-complete', {'version': task.write_date.isoformat(), 'actual_qty': 178, 'confirmed': True}), 'STATE_PRECONDITION_FAILED')
    # Existing production plans can acquire and reassign a pending packaging task.
    legacy, _data = service.create({**payload, 'packaging_plan': {}, 'packaging_owner_id': None})
    service.action(legacy.id, 'set-packaging-plan', {**payload, 'version': legacy.write_date.isoformat()})
    legacy_task = env['brew.task'].search([('target_model', '=', legacy._name), ('target_id', '=', legacy.id), ('task_type', '=', 'packaging')])
    service.action(legacy.id, 'set-packaging-plan', {**payload, 'packaging_owner_id': brewer.id, 'version': legacy.write_date.isoformat()})
    assert legacy_task.assignee_id == brewer
    # Free material lists ignore demand capacity, product customization and available stock.
    material.brew_packaging_details = {'capacity': 500, 'capacity_unit': 'mL', 'custom': True, 'beer_product_id': '999999'}
    carton = env['product.template'].create({'name': prefix + '-carton', 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'packaging_material', 'brew_packaging_type': 'carton', 'brew_packaging_state': 'confirmed', 'brew_packaging_details': {'pack_count': 24}})
    free = {'materials_only': True, 'materials': [{'material_id': material.id, 'quantity': 1000}, {'material_id': carton.id, 'quantity': 3}], 'planned_start': packaging['planned_start'], 'planned_end': packaging['planned_end']}
    values = service._packaging_values({**payload, 'packaging_plan': free}, factory, product, 100, start + timedelta(days=3), SimpleNamespace(packaging_method='keg', package_container_type=False, package_unit_volume_l=10, package_units_per_box=0))
    assert json.loads(values['packaging_plan_json'])['materials'][0]['quantity'] == 1000
    service.action(legacy.id, 'set-packaging-plan', {**payload, 'packaging_plan': free, 'version': legacy.write_date.isoformat()})
    assert len(service.serialize(legacy)['packaging_plan']['materials']) == 2
    legacy.with_context(brew_state_transition=True).write({'state': 'done'})
    legacy_tank = tank.copy({'name':prefix+'Legacy','current_volume_l':0,'current_production_batch_id':False})
    legacy.with_context(brew_state_transition=True).write({'tank_id':legacy_tank.id})
    legacy_batch=env['brew.production.batch'].search([('production_plan_id','=',legacy.id)],limit=1)
    legacy_batch.write({'state':'completed','actual_volume_l':100})
    legacy.with_context(brew_state_transition=True).write({'brewing_completion_json':json.dumps({'actual_volume_l':100,'disposition':'in_tank'})})
    service.action(legacy.id,'confirm-raw-location',{'version':legacy.write_date.isoformat(),'tank_id':legacy_tank.id,'confirmed':True})
    carton_lot=env['stock.lot'].create({'name':prefix+'Carton','product_id':carton.product_variant_id.id,'company_id':company.id,'brew_quality_status':'qualified'})
    env['stock.quant']._update_available_quantity(carton.product_variant_id,source,10,lot_id=carton_lot)
    env.flush_all()
    packer_workflow.task_action(legacy_task.id, 'packaging-start', {'version': legacy_task.write_date.isoformat()})
    actual = [{'material_id': material.id, 'quantity': 900}, {'material_id': carton.id, 'quantity': 4}]
    packer_workflow.task_action(legacy_task.id, 'packaging-complete', {'version': legacy_task.write_date.isoformat(), 'confirmed': True, 'actual_qty':100, 'loss_volume_l':0, 'finished_packaging_id':package.id, 'finished_location_id':target.id, 'actual_materials': actual})
    result = service.serialize(legacy)['packaging_plan']['execution']
    assert legacy_task.state == 'done' and [line['quantity'] for line in result['materials']] == [900, 4]
    # Dispatch locks inventory comparison and packaging changes; finishing fermentation unlocks packaging.
    auto_plan, _ = service.create({**payload, 'packaging_plan': free})
    auto_plan.with_context(brew_state_transition=True).write({'state': 'dispatched'})
    rejected(lambda: service.action(auto_plan.id, 'set-packaging-plan', {**payload, 'packaging_plan': free, 'version': auto_plan.write_date.isoformat()}), 'STATE_PRECONDITION_FAILED')
    from odoo.addons.craft_brew_api.services.inventory import InventoryService
    rejected(lambda: InventoryService(env, director, company).plan_inventory(auto_plan.id), 'STATE_PRECONDITION_FAILED')
    auto_tasks = env['brew.task'].search([('target_model', '=', auto_plan._name), ('target_id', '=', auto_plan.id)])
    auto_tasks.filtered(lambda item: item.task_type == 'mashing').write({'state': 'done', 'execution_checkpoints_json': json.dumps([{'code': 'actual_volume', 'state': 'done', 'value': 20, 'required': True}])})
    auto_batch = env['brew.production.batch'].search([('production_plan_id', '=', auto_plan.id)], limit=1)
    ferment = auto_tasks.filtered(lambda item: item.task_type == 'fermentation')
    now = fields.Datetime.now()
    from odoo.addons.craft_brew_api.services.fermentation_tasks import build_schedule
    schedule = build_schedule({'target_days': 1, 'steps': []}, now, 'Asia/Shanghai')
    ferment.write({'state': 'in_progress', 'execution_checkpoints_json': json.dumps(schedule)})
    WorkflowService(env, brewer, company).task_action(ferment.id, 'submit', {'version': ferment.write_date.isoformat(), 'confirmed': True, 'allow_incomplete': True})
    assert auto_plan.state == 'done' and json.loads(auto_plan.brewing_completion_json)['automatic']
    assert auto_batch.actual_volume_l == 20
    auto_batch.actual_volume_l = 18
    packer_workflow.sync_recorded_batch_volume(auto_plan)
    assert auto_batch.actual_volume_l == 18
    pack = auto_tasks.filtered(lambda item: item.task_type == 'packaging')
    assert not packer_workflow.serialize_task(pack)['execution_blocked_reason']
    packer_workflow.task_action(pack.id, 'packaging-start', {'version': pack.write_date.isoformat()})
    assert pack.state == 'in_progress'
    print('PASS: packaging plan creation, validation, assigned task visibility, start gating, owner-only execution, actual volume checks, immutable started plans, legacy-plan configuration and reassignment')


try:
    run(env)
finally:
    env.cr.rollback()
