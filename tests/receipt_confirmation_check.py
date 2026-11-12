"""Run in Odoo shell; every fixture and stock movement is rolled back."""
import json
import uuid
from unittest.mock import patch
from odoo import Command
from odoo.exceptions import ValidationError
from odoo.addons.craft_brew_api.services.purchase import PurchaseService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService


def run(env):
    code = 'RC' + uuid.uuid4().hex[:3].upper()
    company, user = env.company, env.user
    user.write({'groups_id': [Command.link(env.ref('craft_brew_core.group_brew_admin').id)]})
    factory = env['brew.factory'].create({'name': code, 'code': code, 'company_id': company.id})
    warehouse = env['stock.warehouse'].create({'name': code, 'code': code, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_warehouse_type': 'raw_material'})
    factory.stock_warehouse_id = warehouse
    qc = env['stock.location'].create({'name': 'QC', 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': 'qc', 'location_id': warehouse.lot_stock_id.id})
    dest = env['stock.location'].create({'name': 'A01', 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': 'raw_material', 'location_id': warehouse.lot_stock_id.id})
    supplier = env['res.partner'].create({'name': code, 'supplier_rank': 1})
    product = env['product.template'].create({'name': code, 'company_id': company.id, 'detailed_type': 'product', 'tracking': 'lot', 'brew_item_type': 'raw_material', 'brew_material_category': 'malt'}).product_variant_id
    service = PurchaseService(env, user, company)
    workflow = WorkflowService(env, user, company)
    env['brew.configuration.rule'].create({'name': code, 'code': 'purchase_overreceipt', 'rule_kind': 'approval', 'company_id': company.id, 'factory_id': factory.id, 'state': 'active', 'config_json': json.dumps({'roles': ['tenant_admin'], 'conditions': {}})})

    def order():
        return env['purchase.order'].create({'partner_id': supplier.id, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_approval_state': 'approved', 'state': 'purchase', 'order_line': [Command.create({'product_id': product.id, 'name': product.name, 'product_qty': 10, 'product_uom': product.uom_id.id, 'price_unit': 10})]})

    def payload(po, qty=4, confirm=True):
        return {'purchase_order_id': po.id, 'qc_location_id': qc.id, 'destination_location_id': dest.id, 'confirm_arrival': confirm, 'lines': [{'order_line_id': po.order_line.id, 'received_qty': qty, 'destination_location_id': dest.id, 'supplier_lot': 'SUP-01'}]}

    po = order()
    receipt, data = service.create_receipt(payload(po))
    assert data['state'] == 'arrived' and receipt.quality_inspection_id and receipt.picking_id
    assert receipt.line_ids.lot_id and receipt.line_ids.lot_id.brew_quality_status == 'pending'
    assert not receipt.completed_at, 'confirmation must not bypass QC or put-away'
    second, _ = service.create_receipt(payload(po, 6))
    assert second.state == 'arrived' and second.line_ids.lot_id != receipt.line_ids.lot_id
    qty = sum(env['stock.quant'].search([('location_id', '=', qc.id), ('product_id', '=', product.id)]).mapped('quantity'))
    assert qty == 10, qty
    try:
        service.receipt_action(receipt.id, 'confirm-arrival', {'version': service._version(receipt)})
    except ValidationError:
        pass
    else:
        raise AssertionError('Confirmed receipt accepted twice')

    legacy, _ = service.create_receipt(payload(order(), 3, False))
    assert legacy.state == 'draft' and not legacy.quality_inspection_id
    service.update_receipt(legacy.id, {'version': service._version(legacy), 'confirm_arrival': True, 'note': 'continue legacy draft'})
    assert legacy.state == 'arrived' and legacy.quality_inspection_id

    excess, _ = service.create_receipt(payload(order(), 12))
    assert excess.state == 'pending_approval' and not excess.picking_id and not excess.quality_inspection_id
    step = excess.approval_instance_id.step_ids.filtered(lambda step: step.state == 'pending')[:1]
    workflow.approval_action(step.id, 'approve', {'version': step.write_date.isoformat(), 'comment': 'approved'})
    assert excess.state == 'arrived' and excess.quality_inspection_id, 'approval must finish arrival automatically'

    returned, _ = service.create_receipt(payload(order(), 12))
    step = returned.approval_instance_id.step_ids.filtered(lambda step: step.state == 'pending')[:1]
    workflow.approval_action(step.id, 'return', {'version': step.write_date.isoformat(), 'comment': 'reduce quantity'})
    line = returned.line_ids
    service.update_receipt(returned.id, {'version': service._version(returned), 'confirm_arrival': True, 'lines': [{'id': line.id, 'order_line_id': line.purchase_order_line_id.id, 'received_qty': 8, 'destination_location_id': dest.id}]})
    assert returned.state == 'arrived' and returned.quality_inspection_id, 'corrected return must confirm without a second click'

    before = env['brew.receipt'].search_count([('factory_id', '=', factory.id)])
    with patch.object(type(env['brew.receipt']), 'action_confirm_arrival', side_effect=ValidationError('test confirmation failure')):
        try:
            service.create_receipt(payload(order()))
        except ValidationError:
            pass
        else:
            raise AssertionError('Confirmation failure swallowed')
    assert env['brew.receipt'].search_count([('factory_id', '=', factory.id)]) == before, 'failed confirmation left an orphan draft'
    print('PASS: one-step receipt, partial shipments, QC preservation, duplicate rejection, legacy drafts, over-receipt approval auto-confirmation, returned quantity correction, and atomic rollback')


try:
    run(env)
finally:
    env.cr.rollback()
