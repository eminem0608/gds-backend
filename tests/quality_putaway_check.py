"""Run in Odoo shell; every fixture and stock movement is rolled back."""
import uuid
from unittest.mock import patch
from odoo import Command
from odoo.exceptions import ValidationError
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.purchase import PurchaseService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService


def run(env):
    code = 'QI' + uuid.uuid4().hex[:3].upper()
    company, admin = env.company, env.user
    admin.write({'groups_id': [Command.link(env.ref('craft_brew_core.group_brew_admin').id)]})
    factory = env['brew.factory'].create({'name': code, 'code': code, 'company_id': company.id})
    wh = env['stock.warehouse'].create({'name': code, 'code': code, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_warehouse_type': 'raw_material'})
    factory.stock_warehouse_id = wh
    def location(name, kind):
        return env['stock.location'].create({'name': name, 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_location_type': kind, 'location_id': wh.lot_stock_id.id})
    qc, dest = location('QC', 'qc'), location('A01', 'raw_material')
    supplier = env['res.partner'].create({'name': code, 'supplier_rank': 1})
    product = env['product.template'].create({'name': code, 'company_id': company.id, 'detailed_type': 'product', 'tracking': 'lot', 'brew_item_type': 'raw_material', 'brew_material_category': 'malt'}).product_variant_id
    warehouse_user = env['res.users'].create({'name': code, 'login': code, 'company_id': company.id, 'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])], 'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_warehouse').id])]})
    admin_service = PurchaseService(env, admin, company)
    service = PurchaseService(env, warehouse_user, company)

    def receipt():
        po = env['purchase.order'].create({'partner_id': supplier.id, 'company_id': company.id, 'brew_factory_id': factory.id, 'brew_approval_state': 'approved', 'state': 'purchase', 'order_line': [Command.create({'product_id': product.id, 'name': product.name, 'product_qty': 10, 'product_uom': product.uom_id.id, 'price_unit': 10})]})
        record, _ = admin_service.create_receipt({'purchase_order_id': po.id, 'qc_location_id': qc.id, 'destination_location_id': dest.id, 'confirm_arrival': True, 'lines': [{'order_line_id': po.order_line.id, 'received_qty': 4}]})
        return record

    def results(record, qualified=4, rejected=0, disposition=None, note='checked'):
        inspection = record.quality_inspection_id
        return {'version': service._version(inspection), 'complete_and_put_away': True, 'note': note, 'lines': [{'id': inspection.line_ids.id, 'qualified_qty': qualified, 'rejected_qty': rejected, 'disposition': disposition}]}

    def stock(record, loc):
        return sum(env['stock.quant'].search([('lot_id', '=', record.line_ids.lot_id.id), ('location_id', '=', loc.id)]).mapped('quantity'))

    first = receipt()
    inspection, data = service.update_inspection_results(first.quality_inspection_id.id, results(first))
    assert first.state == 'done' and inspection.state == 'completed'
    assert inspection.inspector_id == warehouse_user and inspection.inspected_at
    assert not inspection.approval_instance_id
    assert stock(first, dest) == 4 and stock(first, qc) == 0
    assert first.line_ids.lot_id.brew_quality_status == 'qualified'
    try:
        service.update_inspection_results(inspection.id, results(first))
    except ApiError as exc:
        assert exc.code == 'STATE_PRECONDITION_FAILED'
    else:
        raise AssertionError('Repeated completion accepted')
    assert stock(first, dest) == 4

    rejected = receipt()
    service.update_inspection_results(rejected.quality_inspection_id.id, results(rejected, 3, 1, 'return'))
    returns = env['brew.purchase.return'].search([('receipt_id', '=', rejected.id)])
    assert rejected.state == 'done' and stock(rejected, dest) == 3 and stock(rejected, qc) == 1
    assert len(returns) == 1 and returns.line_ids.return_qty == 1

    scrapped = receipt()
    service.update_inspection_results(scrapped.quality_inspection_id.id, results(scrapped, 3, 1, 'scrap'))
    assert scrapped.state == 'done' and stock(scrapped, dest) == 3 and stock(scrapped, qc) == 0

    legacy = receipt()
    old = env['brew.approval.instance'].create_for_target('quality_inspection', legacy.quality_inspection_id, admin, factory)
    legacy.quality_inspection_id.write({'state': 'submitted', 'approval_instance_id': old.id})
    approvals, _ = WorkflowService(env, admin, company).list_approvals(1, 100)
    assert not any(item['instance_id'] == str(old.id) for item in approvals)
    service.update_inspection_results(legacy.quality_inspection_id.id, results(legacy))
    assert legacy.state == 'done' and old.state == 'cancelled' and not old.step_ids.filtered(lambda step: step.state == 'pending')

    invalid = receipt()
    try:
        service.update_inspection_results(invalid.quality_inspection_id.id, results(invalid, 3))
    except ValidationError:
        pass
    else:
        raise AssertionError('Incomplete total accepted')
    assert invalid.state == 'arrived' and invalid.quality_inspection_id.state == 'draft'
    assert invalid.quality_inspection_id.line_ids.qualified_qty == 4

    failed = receipt()
    with patch.object(type(failed), 'action_put_away', side_effect=ValidationError('test put-away failure')):
        try:
            service.update_inspection_results(failed.quality_inspection_id.id, results(failed, note='must roll back'))
        except ValidationError:
            pass
        else:
            raise AssertionError('Put-away failure swallowed')
    assert failed.state == 'arrived' and failed.quality_inspection_id.state == 'draft'
    assert not failed.quality_inspection_id.note and not failed.quality_inspection_id.inspector_id
    assert stock(failed, dest) == 0 and stock(failed, qc) == 4

    compatible = receipt()
    service.inspection_action(compatible.quality_inspection_id.id, 'submit', {'version': service._version(compatible.quality_inspection_id)})
    assert compatible.state == 'done' and not compatible.quality_inspection_id.approval_instance_id
    print('PASS: warehouse user directly completes QC and put-away, no review created, mixed reject return/scrap, duplicate protection, historical review retirement, invalid-total rollback, stock-failure atomic rollback, and old submit-client compatibility')


try:
    run(env)
finally:
    env.cr.rollback()
