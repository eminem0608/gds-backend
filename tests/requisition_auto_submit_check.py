"""Run in Odoo shell; fixtures and approval actions are rolled back."""
from unittest.mock import patch
from odoo import fields
from odoo.addons.craft_brew_api.services.purchase import PurchaseService

try:
    user = env.ref('base.user_admin')
    service = PurchaseService(env, user, user.company_id)
    factory = env['brew.factory'].search([('company_id', '=', user.company_id.id), ('active', '=', True)], limit=1)
    assert factory, 'Test requires a configured factory'
    product = env['product.template'].create({'name': 'Auto-submit test malt', 'company_id': user.company_id.id, 'brew_item_type': 'raw_material', 'brew_material_category': 'malt'})
    payload = {'request_type': 'emergency', 'factory_id': factory.id, 'need_date': fields.Date.today().isoformat(), 'emergency_reason': 'Auto-submit regression test', 'lines': [{'product_id': product.product_variant_id.id, 'requested_qty': 1, 'uom_id': product.uom_id.id}]}
    record, data = service.create_requisition(payload)
    assert record.state == 'submitted' and data['state'] == 'submitted'
    approval = record.approval_instance_id
    assert approval and approval.state == 'pending'
    assert record.action_submit(user) == approval, 'Repeated submit must reuse pending approval'
    before = env['brew.purchase.requisition'].search_count([])
    try:
        with env.cr.savepoint(), patch.object(type(record), 'action_submit', side_effect=ValueError('Submission blocked')):
            service.create_requisition(payload)
    except ValueError:
        pass
    else:
        raise AssertionError('Submission failure was swallowed')
    assert env['brew.purchase.requisition'].search_count([]) == before, 'Failed submission must roll back new request'
    print('PASS: create returns submitted, pending approval created, repeated submit idempotent, failed submit rolls back creation')
finally:
    env.cr.rollback()
