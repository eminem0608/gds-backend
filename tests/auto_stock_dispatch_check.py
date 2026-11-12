"""Run in Odoo shell. All stock postings, plans and automation effects roll back."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from odoo import Command, fields
from odoo.addons.craft_brew_api.services.purchase import PurchaseService
from odoo.addons.craft_brew_api.services.planning import PlanningService
from odoo.addons.craft_brew_api.services.inventory import InventoryService
from odoo.addons.craft_brew_api.controllers.base import ApiError


def run(env):
    code = 'AS' + uuid.uuid4().hex[:4].upper()
    company, admin = env.company, env.ref('base.user_admin')
    factory = env['brew.factory'].create({'name': code, 'code': code, 'company_id': company.id})
    wh = env['stock.warehouse'].create({'name': code, 'code': code, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_warehouse_type': 'raw_material'})
    factory.stock_warehouse_id = wh
    def location(name, kind):
        return env['stock.location'].create({'name': name, 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': kind, 'location_id': wh.lot_stock_id.id})
    qc, dest = location('QC', 'qc'), location('A01', 'raw_material')
    supplier = env['res.partner'].create({'name': code, 'supplier_rank': 1})
    raw = env['product.template'].create({'name': code, 'company_id': company.id, 'detailed_type': 'product', 'tracking': 'lot', 'brew_item_type': 'raw_material', 'brew_material_category': 'malt'}).product_variant_id
    finished = env['product.template'].create({'name': code + '-beer', 'company_id': company.id, 'detailed_type': 'product', 'brew_item_type': 'finished_product'})
    warehouse_user = env['res.users'].create({'name': code, 'login': code, 'company_id': company.id, 'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])], 'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_warehouse').id])]})
    purchase = PurchaseService(env, warehouse_user, company)
    start = fields.Datetime.now() + timedelta(days=20)
    def plan(name, required=4, owner=True, material=raw, state='stocking'):
        equipment = env['maintenance.equipment'].create({'name': code + name, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_equipment_type': 'mash'})
        tank = env['brew.tank'].create({'name': code + name, 'factory_id': factory.id, 'capacity_l': 2000})
        recipe = env['brew.recipe'].create({'name': code + name, 'code': code + name, 'company_id': company.id, 'product_tmpl_id': finished.id, 'equipment_id': equipment.id})
        version = env['brew.recipe.version'].create({'recipe_id': recipe.id, 'version': 1, 'base_volume_l': 20, 'state': 'active'})
        return env['brew.production.plan'].create({'name': code + name, 'company_id': company.id, 'factory_id': factory.id, 'product_tmpl_id': finished.id, 'recipe_version_id': version.id, 'planned_volume_l': 20, 'planned_start': start, 'planned_end': start + timedelta(days=2), 'equipment_id': equipment.id, 'tank_id': tank.id, 'mash_owner_id': admin.id if owner else False, 'fermentation_owner_id': admin.id if owner else False, 'state': state, 'material_snapshot_json': json.dumps({'materials': [{'material_id': str(material.id), 'material_name': material.name, 'uom_id': str(material.uom_id.id), 'quantity': required}]}), 'stocking_request_json': json.dumps({'submitted_at': start.isoformat(), 'source': 'production_shortages'})})
    def receipt(quantity):
        order = env['purchase.order'].create({'partner_id': supplier.id, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_approval_state': 'approved', 'state': 'purchase', 'order_line': [Command.create({'product_id': raw.id, 'name': raw.name, 'product_qty': quantity, 'product_uom': raw.uom_id.id, 'price_unit': 10})]})
        record, _ = purchase.create_receipt({'purchase_order_id': order.id, 'qc_location_id': qc.id, 'destination_location_id': dest.id, 'confirm_arrival': True, 'lines': [{'order_line_id': order.order_line.id, 'received_qty': quantity}]})
        return record
    def qualify(record, qualified=None):
        inspection = record.quality_inspection_id
        qty = record.line_ids.received_qty
        qualified = qty if qualified is None else qualified
        return purchase.update_inspection_results(inspection.id, {'version': purchase._version(inspection), 'complete_and_put_away': True, 'lines': [{'id': inspection.line_ids.id, 'qualified_qty': qualified, 'rejected_qty': qty-qualified, 'disposition': 'return' if qty>qualified else None}]})
    def status(record):
        record.invalidate_recordset()
        return json.loads(record.stocking_request_json)['auto_dispatch']
    def tasks(record):
        return env['brew.task'].search([('target_model', '=', record._name), ('target_id', '=', record.id), ('state', '!=', 'cancelled')])

    first, competitor = plan('first'), plan('second')
    arrived = receipt(4)
    env['brew.production.plan']._auto_dispatch_for_factory(factory)
    assert first.state == competitor.state == 'stocking', 'Uninspected stock triggered dispatch'
    assert status(first)['state'] == 'waiting_stock'
    qualify(arrived)
    first.invalidate_recordset(); competitor.invalidate_recordset()
    assert arrived.state == 'done' and first.state == 'dispatched' and competitor.state == 'stocking'
    assert set(tasks(first).mapped('task_type')) == {'mashing', 'fermentation', 'material_preparation'}
    assert all(t.state in {'pending','claimed'} for t in tasks(first)), 'Auto dispatch started execution'
    assert status(first)['state'] == 'dispatched'
    assert json.loads(first.stocking_request_json)['completed_at']
    assert env['brew.outbox.event'].search_count([('event_type','=','production_plan.dispatch'),('aggregate_id','=',str(first.id))]) == 1
    assert env['brew.audit.log'].search_count([('action','=','auto_dispatch'),('resource_id','=',str(first.id))]) == 1
    reservations = env['brew.inventory.reservation'].search([('source_model','=',first._name),('source_id','=',first.id),('state','=','active')])
    assert sum(reservations.mapped('quantity_base')) == 4
    task_ids, reservation_ids = tasks(first).ids, reservations.ids
    env['brew.production.plan']._auto_dispatch_for_factory(factory)
    assert tasks(first).ids == task_ids
    assert env['brew.inventory.reservation'].browse(reservation_ids).mapped('state') == ['active']

    # Partial inspection and the next receipt only dispatch after all stock is ready.
    partial = receipt(4); qualify(partial, 2)
    competitor.invalidate_recordset(); assert competitor.state == 'stocking'
    final = receipt(2); qualify(final)
    competitor.invalidate_recordset(); assert competitor.state == 'dispatched'

    # An actionable conflict must not roll back successful QC or reserve stock.
    blocked = plan('blocked', owner=False)
    arrived = receipt(4); qualify(arrived)
    assert arrived.state == 'done' and blocked.state == 'stocking'
    assert status(blocked)['state'] == 'blocked' and '负责人' in status(blocked)['reason']
    assert not tasks(blocked)
    assert not env['brew.inventory.reservation'].search_count([('source_model','=',blocked._name),('source_id','=',blocked.id),('state','=','active')])
    blocked.with_context(brew_state_transition=True).write({'mash_owner_id':admin.id,'fermentation_owner_id':admin.id})
    env['brew.production.plan']._auto_dispatch_for_factory(factory)
    assert status(blocked)['state'] == 'dispatched' and blocked.state == 'dispatched'

    conflict = plan('occupied')
    conflict.tank_id.current_volume_l = 10
    arrived = receipt(4); qualify(arrived)
    assert arrived.state == 'done' and status(conflict)['state'] == 'blocked'
    assert '罐' in status(conflict)['reason'] and conflict.state == 'stocking'
    conflict.tank_id.current_volume_l = 0
    env['brew.production.plan']._cron_auto_dispatch_stocked()
    conflict.invalidate_recordset(); assert conflict.state == 'dispatched'

    # Unsubmitted plans remain manual. Failures after reserve roll back that plan only.
    draft = plan('draft', state='draft')
    broken = plan('rollback')
    original = PlanningService._dispatch_directly
    def fail_after_dispatch(service, record):
        original(service, record)
        if record.id == broken.id:
            raise ApiError('TEST_FAILURE', '测试下发失败', 409)
    arrived = receipt(4)
    with patch.object(PlanningService, '_dispatch_directly', fail_after_dispatch):
        qualify(arrived)
    broken.invalidate_recordset()
    assert arrived.state == 'done' and broken.state == 'stocking' and draft.state == 'draft'
    assert not tasks(broken)
    assert status(broken)['code'] == 'TEST_FAILURE'
    assert not env['brew.inventory.reservation'].search_count([('source_model','=',broken._name),('source_id','=',broken.id),('state','=','active')])
    env['brew.production.plan']._auto_dispatch_for_factory(factory)
    broken.invalidate_recordset(); assert broken.state == 'dispatched' and draft.state == 'draft'
    print('PASS: QC triggers auto dispatch; pending/rejected stock excluded; FIFO reservations prevent double use; repeat calls idempotent; conflicts isolate QC, retry after repair; dispatch rollback and draft isolation')

try:
    run(env)
finally:
    env.cr.rollback()
