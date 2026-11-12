"""Run in Odoo shell; catalogue and supplier workflow changes are rolled back."""
import uuid
from datetime import timedelta
from odoo import fields
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.packaging import PackagingService
from odoo.addons.craft_brew_api.services.planning import PlanningService


def run(env):
    admin = env.ref('base.user_admin')
    supplier = env['res.users'].search([('login', '=', 'gys2')], limit=1)
    other = env['res.users'].search([('login', '=', 'gys')], limit=1)
    assert supplier and other and supplier.brew_supplier_id != other.brew_supplier_id
    own = PackagingService(env, supplier, admin.company_id)
    manager = PackagingService(env, admin, admin.company_id)
    schema = own.schema()
    categories = {item['type']: item['code'] for item in schema['categories']}
    types = {item['code']: item for item in schema['types']}
    assert {'capacity','material','height_mm','diameter_mm'} <= {field['key'] for field in types['bottle']['fields']}
    assert {'length_mm','width_mm','height_mm','pack_count','fit_capacity_ml'} <= {field['key'] for field in types['carton']['fields']}
    assert set(types) == {'bottle','can','keg','carton'}
    prefix = 'PACK-SPEC-' + uuid.uuid4().hex[:8]
    common = {'unit': '个', 'procurement_unit': '箱', 'unit_quantity': 12, 'quote_price': 24}
    carton_payload = {'name': prefix + '-carton', 'type': 'carton', 'category_code': categories['carton'], 'details': {**common,
        'length_mm': 250, 'width_mm': 180, 'height_mm': 220, 'dimension_basis': '内尺寸',
        'box_style': '瓦楞纸箱', 'board_layers': '五层', 'flute': 'BC', 'material': '瓦楞纸板',
        'pack_count': 12, 'fit_container_type': '酒瓶', 'fit_capacity_ml': 330}}
    record, data = own.save(carton_payload)
    assert data['details']['dimensions'] == '250 × 180 × 220 mm'
    assert record.brew_supplier_id == supplier.brew_supplier_id and record.brew_packaging_state == 'confirmed'
    def rejected(call, code):
        try:
            with env.cr.savepoint():
                call()
        except ApiError as exc:
            assert exc.code == code, (exc.code, exc.message)
        else:
            raise AssertionError('Invalid specification or supplier access accepted')
    rejected(lambda: PackagingService(env, other, admin.company_id).record(record.id), 'RESOURCE_NOT_FOUND')
    rejected(lambda: own.save({**carton_payload, 'details': {**carton_payload['details'], 'length_mm': -1}}), 'VALIDATION_FORMAT')
    rejected(lambda: own.save({**carton_payload, 'details': {**carton_payload['details'], 'width_mm': ''}}), 'VALIDATION_REQUIRED')
    rejected(lambda: own.save({**carton_payload, 'details': {**carton_payload['details'], 'flute': 'wrong'}}), 'VALIDATION_SELECTION')
    bottle, data = own.save({'name': prefix + '-bottle', 'type': 'bottle', 'category_code': categories['bottle'], 'details': {**common,
        'capacity': 330, 'capacity_unit': 'mL', 'material': '玻璃', 'closure': '26 mm 皇冠口',
        'bottle_shape': '长颈瓶', 'height_mm': 230, 'diameter_mm': 60, 'mouth_diameter_mm': 26, 'weight_g': 190}})
    assert data['details']['height_mm'] == 230 and '高度' in bottle.brew_specification
    assert record.brew_packaging_state == 'confirmed'
    assert record.brew_packaging_catalog_owner_id == supplier.brew_supplier_id
    own.save({**carton_payload, 'version': record.write_date.isoformat()}, record.id)
    assert record.brew_packaging_state == 'confirmed' and record.brew_packaging_catalog_owner_id == supplier.brew_supplier_id
    assert any(item['id'] == str(record.id) for item in own.list(1, 100)['items'])
    assert all(item['id'] != str(record.id) for item in manager.list(1, 100)['items'])
    rejected(lambda: manager.record(record.id), 'RESOURCE_NOT_FOUND')
    from odoo.addons.craft_brew_api.services.master_data import MasterDataService
    from odoo.addons.craft_brew_api.services.purchase import PurchaseService
    assert record.id not in env['product.template'].search(MasterDataService(env, admin, admin.company_id)._domain('materials')).ids
    rejected(lambda: PurchaseService(env, admin, admin.company_id)._product(record.product_variant_id.id), 'DATA_SCOPE_DENIED')
    # Winery creates its own materials independently of supplier catalogues.
    record, _data = manager.save(carton_payload)
    bottle, _data = manager.save({'name': prefix + '-winery-bottle', 'type': 'bottle', 'category_code': categories['bottle'], 'details': {**common, 'capacity': 330, 'capacity_unit': 'mL', 'material': '玻璃', 'height_mm': 230, 'diameter_mm': 60}})
    assert not record.brew_packaging_catalog_owner_id and not bottle.brew_packaging_catalog_owner_id
    planning = PlanningService(env, admin, admin.company_id)
    now = fields.Datetime.now()
    packaging = {'packaging_owner_id': 8, 'packaging_plan': {'material_id': bottle.id, 'packaging_method': 'box', 'units_per_box': 12, 'planned_qty': 10, 'planned_start': now.isoformat(), 'planned_end': (now + timedelta(hours=1)).isoformat(), 'materials': [{'material_id': record.id, 'quantity': 10}]}}
    planning._packaging_values(packaging, env['brew.factory'].browse(1), env['product.template'].browse(52), 100, now)
    record.brew_packaging_details = {**record.brew_packaging_details, 'fit_capacity_ml': 500}
    rejected(lambda: planning._packaging_values(packaging, env['brew.factory'].browse(1), env['product.template'].browse(52), 100, now), 'VALIDATION_RELATION')
    print('PASS: detailed bottle/carton specifications, dimension normalization, numeric/select validation, immediate availability, supplier catalogue isolation and independent winery materials')


try:
    run(env)
finally:
    env.cr.rollback()
