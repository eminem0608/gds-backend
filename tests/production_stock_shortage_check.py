"""Run in Odoo shell; fixtures and business changes are rolled back."""
import json
import uuid
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.planning import PlanningService
from odoo.addons.craft_brew_api.services.purchase import PurchaseService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService


def run(env, workbench_check=None):
    prefix = 'STOCK-LINK-' + uuid.uuid4().hex[:8]
    company = env.company
    factory = env['brew.factory'].create({'name': prefix, 'code': prefix, 'company_id': company.id})
    equipment = env['maintenance.equipment'].create({'name': prefix, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_equipment_type': 'mash'})
    tank = env['brew.tank'].create({'name': prefix, 'factory_id': factory.id, 'capacity_l': 2000, 'cleaning_status': 'clean'})
    finished = env['product.template'].create({'name': prefix, 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'finished_product'})
    recipe = env['brew.recipe'].create({'name': prefix, 'code': prefix, 'company_id': company.id, 'product_tmpl_id': finished.id, 'equipment_id': equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 1000, 'state': 'active'})
    def user(role):
        return env['res.users'].create({'name': prefix + role, 'login': prefix + role, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])],
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})
    director, buyer, warehouse = user('production_director'), user('purchase'), user('warehouse')
    template = env['product.template'].search([('brew_item_type', '=', 'raw_material'), ('company_id', 'in', [False, company.id])], limit=1)
    assert template
    materials = [template.copy({'name': prefix + suffix, 'company_id': company.id, 'default_code': prefix + suffix}).product_variant_id for suffix in ('-A', '-B')]
    rows = [{'material_id': p.id, 'uom_id': p.uom_id.id, 'quantity': 10} for p in materials]
    start = fields.Datetime.now() + timedelta(days=2)
    plan = env['brew.production.plan'].create({'name': prefix, 'company_id': company.id, 'factory_id': factory.id,
        'product_tmpl_id': finished.id, 'recipe_version_id': version.id, 'planned_volume_l': 1000,
        'planned_start': start, 'planned_end': start + timedelta(days=3), 'equipment_id': equipment.id,
        'tank_id': tank.id, 'mash_owner_id': director.id, 'fermentation_owner_id': director.id,
        'material_snapshot_json': json.dumps({'materials': rows})})
    env['brew.production.batch'].create({'production_plan_id':plan.id})
    planning = PlanningService(env, director, company)
    purchase = PurchaseService(env, director, company)
    req_model = env['brew.purchase.requisition']
    before = req_model.search_count([('factory_id', '=', factory.id)])
    planning.action(plan.id, 'request-stock', {'version': plan.write_date.isoformat()})
    assert plan.state == 'stocking'
    assert req_model.search_count([('factory_id', '=', factory.id)]) == before
    assert json.loads(plan.stocking_request_json)['source'] == 'production_shortages'
    demand = env['brew.product.demand'].create({'name': prefix + '-DEMAND', 'company_id': company.id,
        'factory_id': factory.id, 'salesperson_id': director.id, 'product_tmpl_id': finished.id,
        'requested_volume_l': 1000, 'requested_delivery_date': (start + timedelta(days=10)).date(), 'state': 'confirmed'})
    plan.with_context(brew_state_transition=True).write({'demand_id': demand.id})
    demand_sources = purchase.requisition_source_plans({'plan_no': demand.name})
    assert len(demand_sources) == 1 and demand_sources[0]['demand']['name'] == demand.name

    other_factory = env['brew.factory'].create({'name': prefix + '-B', 'code': prefix + '-B', 'company_id': company.id})
    other_location = env['stock.location'].create({'name': prefix + '-B', 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': other_factory.id, 'brew_location_type': 'raw_material'})
    other_lot = env['stock.lot'].create({'name': prefix + '-B', 'product_id': materials[0].id, 'company_id': company.id, 'brew_quality_status': 'qualified'})
    env['stock.quant']._update_available_quantity(materials[0], other_location, 20, lot_id=other_lot)
    env.flush_all()
    other_equipment = env['maintenance.equipment'].create({'name': prefix + '-B', 'company_id': company.id, 'brew_factory_id': other_factory.id, 'brew_equipment_type': 'mash'})
    other_tank = env['brew.tank'].create({'name': prefix + '-B', 'factory_id': other_factory.id, 'capacity_l': 2000, 'cleaning_status': 'clean'})
    other_plan = env['brew.production.plan'].create({'name': prefix + '-B', 'company_id': company.id, 'factory_id': other_factory.id,
        'product_tmpl_id': finished.id, 'recipe_version_id': version.id, 'planned_volume_l': 1000,
        'planned_start': start + timedelta(days=5), 'planned_end': start + timedelta(days=8), 'equipment_id': other_equipment.id,
        'tank_id': other_tank.id, 'mash_owner_id': director.id, 'fermentation_owner_id': director.id,
        'material_snapshot_json': json.dumps({'materials': rows}), 'state': 'stocking'})
    other_warehouse = env['res.users'].create({'name': prefix + '-B-user', 'login': prefix + '-B-user', 'company_id': company.id,
        'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([other_factory.id])],
        'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_warehouse').id])]})
    headquarters = user('general_manager')
    assert {row['id'] for row in PurchaseService(env, warehouse, company).requisition_source_plans({'plan_no': prefix})} == {str(plan.id)}
    other_sources = PurchaseService(env, other_warehouse, company).requisition_source_plans({'plan_no': prefix})
    assert {row['id'] for row in other_sources} == {str(other_plan.id)}
    assert [row['product']['id'] for row in other_sources[0]['materials']] == [str(materials[1].id)]
    assert {row['id'] for row in PurchaseService(env, headquarters, company).requisition_source_plans({'plan_no': prefix})} == {str(plan.id), str(other_plan.id)}
    assert not purchase.requisition_source_plans({'plan_no': prefix, 'factory_id': other_factory.id})

    def sources(filters=None):
        items = purchase.requisition_source_plans({'plan_no': prefix, **(filters or {})})
        return next((item['materials'] for item in items if item['id'] == str(plan.id)), [])

    assert len(sources()) == 2 and all(row['requestable_qty'] == 10 for row in sources())
    # A future plan is visible without date filters and when its window overlaps.
    assert len(sources({'date_from': (start + timedelta(days=1)).date().isoformat(), 'date_to': (start + timedelta(days=1)).date().isoformat()})) == 2
    assert not sources({'date_from': fields.Date.today().isoformat(), 'date_to': fields.Date.today().isoformat()})
    payload = {'need_date': start.date().isoformat(), 'items': [{'plan_id': plan.id, 'product_id': materials[0].id}]}
    requisitions, _ = purchase.aggregate_requisitions(payload)
    assert len(requisitions) == 1 and requisitions.state == 'submitted'
    assert requisitions.line_ids.requested_qty == 10
    assert requisitions.line_ids.allocation_ids.production_plan_id == plan
    source = {int(row['product']['id']): row for row in sources()}
    assert source[materials[0].id]['requestable_qty'] == 0
    assert source[materials[0].id]['linked_requisitions'][0]['id'] == str(requisitions.id)
    assert source[materials[1].id]['requestable_qty'] == 10
    try:
        with env.cr.savepoint():
            purchase.aggregate_requisitions(payload)
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('Duplicate requisition accepted')

    # Changed requirements generate only the remaining quantity.
    rows[0]['quantity'] = 12
    plan.write({'material_snapshot_json': json.dumps({'materials': rows})})
    assert next(row for row in sources() if row['product']['id'] == str(materials[0].id))['requestable_qty'] == 2
    additional, _ = purchase.aggregate_requisitions(payload)
    assert additional.line_ids.requested_qty == 2

    # Stock completion stays blocked until qualified inventory is available.
    try:
        with env.cr.savepoint():
            planning.action(plan.id, 'complete-stock', {'version': plan.write_date.isoformat()})
    except ApiError as exc:
        assert exc.code == 'INVENTORY_INSUFFICIENT'
    else:
        raise AssertionError('Stock completed without inventory')
    location = env['stock.location'].create({'name': prefix, 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id})
    alternate_location = env['stock.location'].create({'name': prefix + '-other', 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': 'raw_material'})
    for product in materials:
        lot = env['stock.lot'].create({'name': prefix + str(product.id), 'product_id': product.id, 'company_id': company.id, 'brew_quality_status': 'qualified'})
        env['stock.quant']._update_available_quantity(product, alternate_location if product == materials[0] else location, 20, lot_id=lot)
    env.flush_all()
    # Gram-scale reservations must not round up to the stock unit's precision.
    reservation = env['brew.inventory.reservation'].create({
        'company_id': company.id, 'factory_id': factory.id,
        'product_id': product.id, 'lot_id': lot.id, 'location_id': location.id,
        'quantity': 0.001, 'uom_id': product.uom_id.id,
        'source_model': plan._name, 'source_id': plan.id,
    })
    assert abs(reservation.quantity_base - 0.001) < 1e-10
    reservation.action_release()
    assert not sources()
    rows[1]['quantity'] = 0.001
    rows.append({**rows[0], 'quantity': 1})  # Same ingredient used at another stage.
    plan.write({'material_snapshot_json': json.dumps({'materials': rows})})
    planning.action(plan.id, 'dispatch', {'version': plan.write_date.isoformat()})
    assert plan.state == 'dispatched' and json.loads(plan.stocking_request_json)['completed_at']
    assert env['brew.task'].search_count([('target_model', '=', plan._name), ('target_id', '=', plan.id), ('task_type', 'in', ['mashing', 'fermentation', 'material_preparation'])]) == 3
    tasks = env['brew.task'].search([('target_model', '=', plan._name), ('target_id', '=', plan.id)])
    assert not env['stock.move'].search_count([('brew_source_model', '=', 'brew.material.requisition'), ('brew_source_id', 'in', env['brew.material.requisition'].search([('production_plan_id', '=', plan.id)]).ids)])
    preparation = tasks.filtered(lambda task: task.task_type == 'material_preparation')
    mash = tasks.filtered(lambda task: task.task_type == 'mashing')
    operator_workflow = WorkflowService(env, director, company)
    try:
        with env.cr.savepoint():
            operator_workflow.task_action(mash.id, 'checkpoint', {'version': mash.write_date.isoformat(), 'checkpoint_key': 'confirm_materials'})
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('Materials acknowledged before warehouse issue')
    destination = env['stock.location'].create({'name': prefix + '-production', 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': 'material_issue'})
    location.brew_location_type = 'raw_material'
    payload = {'version': preparation.write_date.isoformat(), 'production_location_id': destination.id}
    try:
        with env.cr.savepoint():
            operator_workflow.task_action(preparation.id, 'issue-materials', payload)
    except ApiError as exc:
        assert exc.code == 'PERMISSION_DENIED'
    else:
        raise AssertionError('Non-warehouse user issued materials')
    warehouse_workflow = WorkflowService(env, warehouse, company)
    warehouse_workflow.task_action(preparation.id, 'prepare-materials', payload)
    preview = env['brew.material.requisition'].search([('production_plan_id', '=', plan.id)])
    assert preview.state == 'recommended' and preparation.state != 'done'
    assert len(preview.line_ids) == 2
    assert preview.line_ids.filtered(lambda line:line.product_id == materials[0]).planned_qty == 13
    assert not env['stock.move'].search_count([('brew_source_model', '=', preview._name), ('brew_source_id', '=', preview.id)])
    progress = PlanningService(env, warehouse, company).progress(plan.id)
    assert progress['material_flow']['state'] == 'recommended'
    assert progress['material_requisitions'][0]['lines'][0]['allocations']
    warehouse_workflow.task_action(preparation.id, 'issue-materials', {**payload, 'version': preparation.write_date.isoformat()})
    requisition = env['brew.material.requisition'].search([('production_plan_id', '=', plan.id)])
    assert requisition.state == 'issued' and preparation.state == 'done'
    try:
        with env.cr.savepoint():
            plan.action_cancel('cancel with unreturned stock')
    except ValidationError:
        pass
    else:
        raise AssertionError('Plan cancelled with unreturned issued materials')
    assert set(requisition.line_ids.mapped('allocation_ids.location_id').ids) == {location.id, alternate_location.id}
    try:
        with env.cr.savepoint():
            WorkflowService(env, warehouse, company).task_action(preparation.id, 'issue-materials', {**payload, 'version': preparation.write_date.isoformat()})
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('Repeated material issue accepted')
    tiny_line = requisition.line_ids.filtered(lambda line: line.product_id == materials[1])
    assert abs(tiny_line.issued_qty - 0.001) < 1e-10
    env.flush_all()
    quantities = env['stock.quant'].search([('product_id', '=', materials[1].id), ('location_id', '=', location.id)]).mapped('quantity')
    assert abs(sum(quantities) - 19.999) < 1e-8, quantities
    from odoo.addons.craft_brew_api.services.inventory import InventoryService
    candidates = InventoryService(env, director, company)._candidate_stock(materials[1], factory)
    assert abs(sum(item['quantity'] for item in candidates) - 19.999) < 1e-8
    from odoo.addons.craft_brew_api.services.production import ProductionService
    warehouse_service = ProductionService(env, warehouse, company)
    assert operator_workflow.serialize_task(mash)['material_receipt']['can_acknowledge']
    # Even a custom workflow without a material checkpoint cannot bypass receipt.
    mash.execution_checkpoints_json = json.dumps([{'code':'manual-first','title':'开始糖化','input_mode':'confirm','state':'pending'}])
    for action, extra in [('start', {}), ('checkpoint', {'checkpoint_key':'manual-first'}), ('submit', {})]:
        try:
            with env.cr.savepoint():
                operator_workflow.task_action(mash.id, action, {'version':mash.write_date.isoformat(), **extra})
        except ApiError as exc:
            assert exc.code == 'STATE_PRECONDITION_FAILED'
        else:
            raise AssertionError('Production bypassed material acknowledgement')
    # Before acknowledgement warehouse can physically collect the stock and resend.
    warehouse_service.requisition_action(requisition.id, 'undo-issue', {'version': requisition.write_date.isoformat()})
    assert requisition.state == 'recommended' and preparation.state == 'claimed'
    assert not operator_workflow.serialize_task(mash)['material_receipt']['can_acknowledge']
    warehouse_workflow.task_action(preparation.id, 'issue-materials', {'version':preparation.write_date.isoformat()})
    # Explicit receipt also works for custom workflows and does not start production.
    operator_workflow.task_action(mash.id, 'acknowledge-materials', {'version':mash.write_date.isoformat()})
    assert operator_workflow.serialize_task(mash)['material_receipt']['ready']
    assert plan.state == 'dispatched'
    if workbench_check:
        workbench_check(env, plan, mash, warehouse, buyer, director, requisition)
    try:
        with env.cr.savepoint():
            warehouse_service.requisition_action(requisition.id, 'undo-issue', {'version':requisition.write_date.isoformat()})
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('Acknowledged materials were withdrawn')
    operator_workflow.task_action(mash.id, 'checkpoint', {'version':mash.write_date.isoformat(), 'checkpoint_key':'manual-first'})
    assert plan.state == 'in_progress'
    # Resolve references after savepoint rollbacks invalidate the ORM cache.
    tiny_line = requisition.line_ids.filtered(lambda line: line.product_id == materials[1])
    assert requisition.state == 'acknowledged' and abs(tiny_line.acknowledged_qty - 0.001) < 1e-10
    operator_service = ProductionService(env, director, company)
    settle = []
    for line in requisition.line_ids:
        for allocation in line.allocation_ids:
            settle.append({'allocation_id':allocation.id, 'returned_qty':2 if line.product_id == materials[0] else 0, 'loss_qty':1 if line.product_id == materials[0] else 0})
    operator_service.requisition_action(requisition.id, 'settle', {'version':requisition.write_date.isoformat(), 'allocations':settle})
    assert requisition.state == 'completed'
    for line in requisition.line_ids:
        assert abs(line.acknowledged_qty - line.returned_qty - line.consumed_qty - line.loss_qty) < 1e-8
    env.flush_all()
    assert abs(sum(env['stock.quant'].search([('location_id','=',destination.id)]).mapped('quantity'))) < 1e-8
    assert PlanningService(env, warehouse, company).progress(plan.id)['material_flow']['state'] == 'completed'
    plan.action_terminate('test terminal stock closure')
    assert plan.state == 'terminated'
    assert not env['brew.inventory.reservation'].search_count([('source_model','=',plan._name),('source_id','=',plan.id),('state','=','active')])
    print('PASS: picking preview, custom-flow receipt gate, collect/resend, owner receipt, exact return/consumption/loss conservation')
    print('PASS: factory A and B see only local shortages; headquarters sees both; stock is calculated per factory')
    print('PASS: direct dispatch -> warehouse issue -> assignee acknowledgement; exact gram-scale stock deduction and role checks')
    print('PASS: stock request -> production shortages -> selected requisition; duplicate prevention, incremental shortages, date filters, qualified inventory and completion')


if not globals().get('IMPORT_FIXTURE_ONLY'):
    try:
        run(env)
    finally:
        env.cr.rollback()
