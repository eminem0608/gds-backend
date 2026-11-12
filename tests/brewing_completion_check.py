"""Run with Odoo shell; all fixtures and completion changes are rolled back."""
import json
import uuid
from datetime import timedelta
from odoo import Command, fields
from odoo.addons.craft_brew_api.services.planning import PlanningService
from odoo.addons.craft_brew_api.controllers.base import ApiError


def run(env):
    prefix = 'END-' + uuid.uuid4().hex[:8]
    company = env.company
    factory = env['brew.factory'].create({'name': prefix, 'code': prefix, 'company_id': company.id})
    equipment = env['maintenance.equipment'].create({'name': prefix, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_equipment_type': 'mash'})
    tank = env['brew.tank'].create({'name': prefix, 'factory_id': factory.id, 'capacity_l': 2000, 'cleaning_status': 'clean'})
    product = env['product.template'].create({'name': prefix, 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'finished_product'})
    recipe = env['brew.recipe'].create({'name': prefix, 'code': prefix, 'company_id': company.id, 'product_tmpl_id': product.id, 'equipment_id': equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 1000, 'state': 'active'})
    def user(role):
        return env['res.users'].create({'name': prefix + role, 'login': prefix + role, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])],
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})
    director, operator = user('production_director'), user('operator')
    service = PlanningService(env, director, company)
    start = fields.Datetime.now()
    plan = env['brew.production.plan'].create({'name': prefix, 'company_id': company.id, 'factory_id': factory.id,
        'product_tmpl_id': product.id, 'recipe_version_id': version.id, 'planned_volume_l': 1000,
        'planned_start': start, 'planned_end': start + timedelta(days=30), 'equipment_id': equipment.id,
        'tank_id': tank.id, 'state': 'in_progress', 'mash_owner_id': operator.id, 'fermentation_owner_id': operator.id})
    batch = env['brew.production.batch'].create({'production_plan_id': plan.id})
    tasks = env['brew.task']
    for kind in ['mashing', 'fermentation']:
        tasks |= env['brew.task'].create({'name': prefix + kind, 'task_type': kind, 'role_code': 'operator',
            'assignee_id': operator.id, 'company_id': company.id, 'factory_id': factory.id,
            'target_model': plan._name, 'target_id': plan.id, 'state': 'done'})
    payload = {'actual_volume_l': 900, 'loss_volume_l': 100, 'disposition': 'in_tank', 'destination': tank.name, 'confirmed': True}
    from odoo.addons.craft_brew_api.services.fermentation import FermentationService
    from odoo.addons.craft_brew_api.services.tank_context import task_measurements
    tank_service = FermentationService(env, director, company)
    assert tank_service.tank_detail(tank.id)['stage'] == 'finish'
    tasks[0].state = 'in_progress'
    env.flush_all()
    detail = tank_service.tank_detail(tank.id)
    assert detail['stage'] == 'mashing' and detail['production']['plan']['id'] == str(plan.id)
    tasks[0].state = 'done'
    tasks[1].state = 'in_progress'
    env.flush_all()
    assert tank_service.tank_detail(tank.id)['stage'] == 'fermentation'
    tasks[1].state = 'done'
    def rejected(call):
        try:
            call()
        except ApiError:
            return
        raise AssertionError('Expected rejection')
    from odoo.addons.craft_brew_api.services.workflow import WorkflowService
    from odoo.addons.craft_brew_api.services.fermentation_tasks import build_schedule
    fermentation = tasks.filtered(lambda task: task.task_type == 'fermentation')
    data = build_schedule({'target_days': 10, 'steps': [{'condition': 'time', 'schedule_type': 'daily', 'action': 'measure_gravity'}]}, start)
    data['records'] = [
        {'id': 'observed-zero', 'entry_ids': [], 'metrics': {'gravity': 0}, 'measured_at': start.isoformat(), 'submitted_at': start.isoformat(), 'by': operator.name},
        {'id': 'future-temperature', 'entry_ids': [], 'metrics': {'temperature': 99}, 'measured_at': (start + timedelta(days=1)).isoformat(), 'submitted_at': start.isoformat(), 'by': operator.name},
    ]
    fermentation.write({'state': 'in_progress', 'execution_checkpoints_json': json.dumps(data)})
    env.flush_all()
    measured = task_measurements(env, plan)
    assert measured['gravity']['value'] == 0 and 'temperature' not in measured
    workflow = WorkflowService(env, operator, company)
    submit = {'version': fermentation.write_date.isoformat(), 'confirmed': True}
    rejected(lambda: workflow.task_action(fermentation.id, 'submit', submit))
    rejected(lambda: workflow.task_action(fermentation.id, 'submit', {**submit, 'confirmed': False, 'allow_incomplete': True}))
    workflow.task_action(fermentation.id, 'submit', {**submit, 'allow_incomplete': True})
    ended = json.loads(fermentation.execution_checkpoints_json)
    assert fermentation.state == 'done' and ended['completion']['early']
    assert len(ended['completion']['remaining_entry_ids']) == 10
    assert all(entry['record'] is None for entry in ended['entries'])
    rejected(lambda: PlanningService(env, operator, company)._complete_brewing(plan, payload))
    rejected(lambda: service._complete_brewing(plan, {**payload, 'confirmed': False}))
    rejected(lambda: service._complete_brewing(plan, {**payload, 'actual_volume_l': float('nan')}))
    rejected(lambda: service._complete_brewing(plan, {**payload, 'loss_volume_l': -1}))
    rejected(lambda: service._complete_brewing(plan, {**payload, 'destination': ''}))
    rejected(lambda: service._complete_brewing(plan, {**payload, 'actual_volume_l': 2500}))
    tasks[0].state = 'in_progress'
    rejected(lambda: service._complete_brewing(plan, payload))
    tasks[0].state = 'done'
    rejected(lambda: service.action(plan.id, 'complete-brewing', {**payload, 'version': 'stale'}))
    service.action(plan.id, 'complete-brewing', {**payload, 'version': plan.write_date.isoformat()})
    assert plan.state == 'done' and batch.state == 'completed'
    assert tank.status == 'ready' and tank.current_production_batch_id == batch and tank.current_volume_l == 900
    ready_detail = tank_service.tank_detail(tank.id)
    assert ready_detail['stage'] == 'ready' and ready_detail['production']['plan']['brewing_completion']['actual_volume_l'] == 900
    completion = json.loads(plan.brewing_completion_json)
    assert completion['confirmed_by_id'] == str(director.id) and completion['loss_volume_l'] == 100
    assert any(e['title'] == '确认酿酒结束' for e in service.progress(plan.id)['timeline'])
    next_plan = plan.copy({'name': prefix + '-NEXT', 'state': 'draft'})
    assert any(c['type'] == 'tank_occupancy' for c in next_plan.check_resources()['conflicts'])
    rejected(lambda: service._complete_brewing(plan, payload))
    plan.with_context(brew_state_transition=True).write({'state': 'in_progress', 'brewing_completion_json': '{}'})
    batch.state = 'in_tank'
    session = env['brew.fermentation.session'].create({'batch_id': batch.id, 'source_tank_id': tank.id, 'tank_id': tank.id, 'current_volume_l': 900, 'state': 'ready'})
    service.action(plan.id, 'complete-brewing', {**payload, 'disposition': 'emptied', 'destination': '包装接收', 'version': plan.write_date.isoformat()})
    assert plan.state == 'done' and session.state == 'completed'
    assert tank.status == 'empty' and tank.cleaning_status == 'dirty' and not tank.current_production_batch_id and tank.current_volume_l == 0
    assert session.current_volume_l > 0  # historical volume retains the session SQL constraint
    assert not any(c['type'] == 'tank_occupancy' for c in next_plan.check_resources()['conflicts'])
    empty_detail = tank_service.tank_detail(tank.id)
    assert empty_detail['stage'] == 'empty' and not empty_detail['production'] and empty_detail['history']
    print('PASS: permissions, prerequisite tasks, numeric validation, version conflicts, retained occupancy, drained tank, session closure, audit timeline, duplicate rejection')

try:
    run(env)
finally:
    env.cr.rollback()
