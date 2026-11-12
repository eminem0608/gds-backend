"""Run in Odoo shell; fixtures are rolled back and real business data is read-only."""
import uuid
from datetime import timedelta
from odoo import Command, fields
from odoo.addons.craft_brew_api.services.reporting import ReportingService
from odoo.addons.craft_brew_api.controllers.base import ApiError

try:
    company = env.company
    prefix = 'DASH-' + uuid.uuid4().hex[:8]
    factories = [env['brew.factory'].create({'name': prefix + str(i), 'code': prefix + str(i), 'company_id': company.id}) for i in range(2)]
    locations = [env['stock.location'].create({'name': prefix + str(i), 'usage': 'internal', 'company_id': company.id, 'brew_factory_id': f.id, 'brew_location_type': 'raw_material'}) for i, f in enumerate(factories)]
    product = env['product.product'].search([('company_id','=',company.id),('detailed_type','=','product')],limit=1)
    assert product
    today = fields.Date.context_today(env.user.with_context(tz='Asia/Shanghai'))
    for i, (location, offset, quality) in enumerate([(locations[0], -1, 'qualified'), (locations[0], 10, 'qualified'), (locations[0], 100, 'pending'), (locations[1], -1, 'qualified')]):
        lot = env['stock.lot'].create({'name': prefix + str(i), 'product_id': product.id, 'company_id': company.id, 'brew_expiration_date': today + timedelta(days=offset), 'brew_quality_status': quality})
        env['stock.quant']._update_available_quantity(product, location, 1, lot_id=lot)
    def user(role):
        return env['res.users'].create({'name': prefix + role, 'login': prefix + role, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factories[0].id])],
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_' + role).id])]})
    env.flush_all()
    director = user('production_director')
    service = ReportingService(env,director,company)
    result = service.dashboard()
    assert result['inventory']['expired'] == 1 and result['inventory']['expiring'] == 1
    assert result['metrics']['expiring_lots'] == 1
    assert result['inventory']['pending_quality'] == 1
    assert [row['id'] for row in result['factories']] == [str(factories[0].id)]
    try:
        service.dashboard(factories[1].id)
        raise AssertionError('Unauthorized factory accepted')
    except ApiError:
        pass
    for role in ['purchase','sales','finance']:
        data = ReportingService(env,user(role),company).dashboard()
        assert not data['sections']['production'] and not data['tanks'] and not data['alerts']
        assert 'production' not in data and 'ready_volume_l' not in data['metrics']
        if role == 'sales': assert data['sections']['delivery'] and 'purchase' not in data and 'inventory' not in data
        else: assert data['sections']['purchase'] and 'delivery' not in data
    admin = ReportingService(env,env.ref('base.user_admin'),company).dashboard(1)
    assert len(admin['production']['trend']) == 7
    completed = env['brew.production.batch'].search([('company_id','=',company.id),('factory_id','=',1),('state','=','completed'),('completed_at','!=',False)])
    expected = sum(b.actual_volume_l for b in completed if today.replace(day=1) <= fields.Datetime.context_timestamp(env.user.with_context(tz='Asia/Shanghai'),b.completed_at).date() <= today)
    assert admin['production']['month_volume_l'] == expected
    count = env['brew.production.anomaly'].search_count([('company_id','=',company.id),('factory_id','=',1),('state','!=','closed')])
    assert admin['metrics']['open_anomalies'] == count
    print('PASS: factory isolation, expired/expiring separation, stock-backed lots only, sales/purchase/finance permissions, seven-day series, actual production volume, complete anomaly count')
finally:
    env.cr.rollback()
