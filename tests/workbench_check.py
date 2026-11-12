"""Run in Odoo shell, alongside production_stock_shortage_check.py. All changes roll back."""
import json
from datetime import timedelta
from pathlib import Path
from odoo import fields
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.workbench import WorkbenchService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService
from odoo.addons.craft_brew_api.services.purchase import PurchaseService
from odoo.addons.craft_brew_api.services.fermentation_tasks import build_schedule


def check(env, plan, mash, warehouse, buyer, operator, requisition):
    demo = env['brew.task'].create({'name':'演示准备任务','company_id':plan.company_id.id,'factory_id':plan.factory_id.id,'task_type':'production_preparation','target_model':'seed.dev.data','target_id':1,'role_code':'operator','assignee_id':operator.id})
    assert not any(i['id'] == f'task:{demo.id}' for i in WorkbenchService(env, operator, operator.company_id).get()['items'])
    original = mash.execution_checkpoints_json
    now = fields.Datetime.now()
    # Main rest and parallel water preparation can proceed independently.
    workflow = WorkflowService(env, operator, operator.company_id)
    parallel_nodes = [
        {'code':'mash_in','title':'投料完成','state':'done','completed_at':now.isoformat(),'completed_by':operator.name},
        {'code':'prepare_sparge_water','title':'准备洗糟水','state':'pending','parallel_after':'mash_in','input_mode':'confirm'},
        {'code':'rest_0_start','title':'开始保温','state':'pending','input_mode':'number','temperature':65},
        {'code':'rest_0_end','title':'保温结束','state':'pending','timer_from':'rest_0_start','due_after_min':10},
        {'code':'sparge_1','title':'第一次洗糟','state':'pending','input_mode':'confirm'},
    ]
    saved_state, saved_started = mash.state, mash.started_at
    mash.execution_checkpoints_json = json.dumps(parallel_nodes)
    view = WorkbenchService(env, operator, operator.company_id).get()
    keys = [i.get('checkpoint_key') for i in view['items'] if i.get('task_id') == str(mash.id)]
    assert keys == ['rest_0_start', 'prepare_sparge_water'], keys
    workflow._confirm_mash_checkpoint(mash, {'checkpoint_key':'rest_0_start','value':65})
    current_nodes = json.loads(mash.execution_checkpoints_json)
    assert current_nodes[1]['state'] == 'pending' and current_nodes[2]['state'] == 'done'
    current_nodes[3].update(state='done',completed_at=now.isoformat(),completed_by=operator.name)
    mash.execution_checkpoints_json = json.dumps(current_nodes)
    try:
        with env.cr.savepoint():
            workflow._confirm_mash_checkpoint(mash, {'checkpoint_key':'sparge_1'})
    except ApiError as exc:
        assert '准备洗糟水' in str(exc)
    else:
        raise AssertionError('未完成备水即开始洗糟')
    workflow._confirm_mash_checkpoint(mash, {'checkpoint_key':'prepare_sparge_water'})
    workflow._confirm_mash_checkpoint(mash, {'checkpoint_key':'sparge_1'})
    mash.write({'execution_checkpoints_json':original,'state':saved_state,'started_at':saved_started})
    mash.write({'execution_checkpoints_json': json.dumps([
        {'code': 'rest_0_start', 'title': '达温', 'state': 'done', 'completed_at': now.isoformat(), 'completed_by': operator.name, 'completed_by_id': str(operator.id)},
        {'code': 'rest_0_end', 'title': '保温结束', 'state': 'pending', 'timer_from': 'rest_0_start', 'due_after_min': 10, 'input_mode': 'confirm'}
    ])})
    workflow = WorkflowService(env, operator, operator.company_id)
    data = WorkbenchService(env, operator, operator.company_id).get()
    assert any(i['task_id'] == str(mash.id) for i in data['timers'])
    assert data['pending_count'] == len(data['items']) + len(data['timers'])
    product_id = str(plan.product_tmpl_id.id)
    assert any(g['id'] == product_id and g['count'] >= 1 for g in data['today_products'])
    assert sum(g['count'] for g in data['today_products']) == data['pending_count']
    assert data['product_calendar'][product_id][data['today']] >= 1
    assert sum(b['count'] for b in data['today_batches']) == data['pending_count']
    assert data['batch_calendar']['plan:' + plan.name][data['today']] >= 1
    assert any(b['production']['plan_name'] == plan.name for b in data['history_batches'] if b.get('production'))
    assert any(g['id'] == product_id and g['count'] >= 1 for g in data['history_products'])
    assert any(r['title'] == '达温' for r in data['records'])
    try:
        with env.cr.savepoint():
            workflow.task_action(mash.id, 'checkpoint', {'version': mash.write_date.isoformat(), 'checkpoint_key': 'rest_0_end'})
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('保温未到时间即被完成')
    nodes = json.loads(mash.execution_checkpoints_json)
    nodes[0]['completed_at'] = (now - timedelta(minutes=11)).isoformat()
    mash.write({'execution_checkpoints_json': json.dumps(nodes)})
    workflow.task_action(mash.id, 'checkpoint', {'version': mash.write_date.isoformat(), 'checkpoint_key': 'rest_0_end'})
    assert any(r['title'] == '保温结束' for r in WorkbenchService(env, operator, operator.company_id).get()['records'])
    mash.write({'execution_checkpoints_json': original, 'state': 'pending'})
    # The warehouse sees real, executable work only in its allowed factory.
    scope = WorkbenchService(env, warehouse, warehouse.company_id).get()
    for item in scope['items']:
        if item.get('plan_id'):
            assert env['brew.production.plan'].browse(int(item['plan_id'])).factory_id in warehouse.brew_factory_ids
    assert not any(i.get('task_id') == str(mash.id) for i in scope['items'])
    # A different procurement account cannot take another buyer's draft order.
    supplier = env['res.partner'].create({'name': 'WB Supplier', 'supplier_rank': 1})
    product = requisition.line_ids[0].product_id
    po = env['purchase.order'].create({'partner_id': supplier.id, 'company_id': plan.company_id.id, 'brew_factory_id': plan.factory_id.id, 'user_id': buyer.id,
        'order_line': [(0,0,{'product_id': product.id, 'name': product.name, 'product_qty': 1, 'product_uom': product.uom_id.id, 'price_unit': 1})]})
    items = WorkbenchService(env, buyer, buyer.company_id).get()['items']
    assert any(i['id'] == f'order:{po.id}:edit' for i in items)
    assert not any(i['id'].startswith(f'order:{po.id}:') for i in WorkbenchService(env, warehouse, warehouse.company_id).get()['items'])
    # Atomic save-and-submit is used by the task drawer, while ordinary save is unchanged.
    po.brew_approval_state = 'approved'
    items = WorkbenchService(env, buyer, buyer.company_id).get()['items']
    assert any(i['id'] == f'order:{po.id}:place' for i in items)
    po.brew_approval_state = 'none'
    saved, _ = PurchaseService(env, buyer, buyer.company_id).update_order(po.id, {
        'version': po.write_date.isoformat(), 'expected_arrival_date': (now + timedelta(days=3)).date().isoformat(), 'submit_after_save': True})
    assert saved.brew_approval_state == 'pending'
    assert not any(i['id'].startswith(f'order:{po.id}:') for i in WorkbenchService(env, buyer, buyer.company_id).get()['items'])
    # Future fermentation measurements are neither executable nor saved as history.
    mash.state = 'done'
    fermentation = env['brew.task'].create({'name':'WB fermentation','company_id':plan.company_id.id,'factory_id':plan.factory_id.id,'task_type':'fermentation','target_model':plan._name,'target_id':plan.id,'assignee_id':operator.id,'state':'in_progress'})
    schedule = build_schedule({'target_days':3,'steps':[{'condition':'time','schedule_type':'daily','action':'measure_gravity'}]}, now)
    fermentation.execution_checkpoints_json = json.dumps(schedule)
    tomorrow = next(e for e in schedule['entries'] if e['day'] == 2)
    view = WorkbenchService(env, operator, operator.company_id).get()
    assert any(i.get('entry_id') == tomorrow['id'] and i.get('task_id') == str(fermentation.id) for i in view['tomorrow'])
    try:
        with env.cr.savepoint():
            workflow._record_fermentation(fermentation, {'entry_ids':[tomorrow['id']], 'metrics':{'gravity':4}, 'measured_at':(now+timedelta(days=1)).isoformat()})
    except ApiError:
        pass
    else:
        raise AssertionError('明日测量被提前执行')
    mash.state = 'pending'
    fermentation.state = 'cancelled'
    print('PASS: personal scopes, procurement edit/place transition, task counts, calendar records, server timers, tomorrow schedule and future-record rejection')

namespace = {'IMPORT_FIXTURE_ONLY': True}
fixture = Path('/tmp/production_stock_shortage_check.py')
exec(compile(fixture.read_text(), str(fixture), 'exec'), namespace)
try:
    namespace['run'](env, check)
finally:
    env.cr.rollback()
