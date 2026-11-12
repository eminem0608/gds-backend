"""Bridge assigned packaging tasks to physical consumption and release documents."""
import json
import math
from odoo import Command, fields
from ..controllers.base import ApiError
from .inventory import InventoryService


def register_output(env, user, company, plan, task, data, payload, repair=False):
    inventory = InventoryService(env, user, company, enforce_access=False)
    batch = env['brew.production.batch'].sudo().search([('production_plan_id', '=', plan.id)], limit=1)
    if not batch:
        raise ApiError('STATE_PRECONDITION_FAILED', '本计划尚无实际生产批次。', 422)
    if env['brew.packaging.order'].sudo().search_count([('task_id', '=', task.id)]):
        raise ApiError('STATE_PRECONDITION_FAILED', '该任务已登记包装产出，请勿重复提交。', 409)
    try:
        qty = float(payload.get('actual_qty'))
        loss = float(payload.get('loss_volume_l', 0))
        if any(isinstance(payload.get(key), bool) for key in ('actual_qty', 'loss_volume_l')) or not math.isfinite(qty) or not qty.is_integer() or qty <= 0 or not math.isfinite(loss) or loss < 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise ApiError('VALIDATION_RANGE', '请填写正整数包装数量和非负损耗量。', 422)
    manual_output = 'output_volume_l' in payload
    product_name = str(payload.get('finished_product_name') or '').strip()
    output_unit = str(payload.get('output_unit') or '')
    if manual_output:
        try:
            volume = float(payload.get('output_volume_l'))
            if isinstance(payload.get('output_volume_l'), bool) or not math.isfinite(volume) or volume <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ApiError('VALIDATION_RANGE', '请填写大于零的实际产出酒液量。', 422)
        if not product_name or len(product_name) > 200 or output_unit not in {'can','bottle','keg','box'}:
            raise ApiError('VALIDATION_REQUIRED', '请填写成品名称并选择包装单位。', 422)
        # Store a per-output snapshot for existing release and traceability records.
        # Quantity and volume are both operator-entered; never infer reported volume.
        package = env['product.packaging'].sudo().create({
            'name': product_name + '（' + {'can':'罐','bottle':'瓶','keg':'桶','box':'箱'}[output_unit] + '）',
            'product_id': plan.product_tmpl_id.product_variant_id.id,
            'company_id': company.id, 'qty': 1,
            'brew_barcode_level': output_unit, 'brew_volume_l': volume / qty,
            'active': False,
        })
    else:
        package_id = payload.get('finished_packaging_id') or data.get('finished_packaging_id') or data.get('packaging_id')
        try:
            package_id = int(package_id or 0)
        except (TypeError, ValueError):
            package_id = 0
        package = env['product.packaging'].sudo().search([('id', '=', package_id), ('product_id.product_tmpl_id', '=', plan.product_tmpl_id.id), ('company_id', 'in', [False, company.id]), ('active', '=', True), ('brew_volume_l', '>', 0)], limit=1)
        if not package:
            raise ApiError('VALIDATION_REQUIRED', '请先选择本酒款的包装规格，以核对实际产出容量。', 422)
    location = inventory._location(payload.get('finished_location_id'))
    if location.brew_factory_id != plan.factory_id or location.usage != 'internal' or location.brew_location_type != 'finished':
        raise ApiError('VALIDATION_RELATION', '入库位置须为本厂区的成品库位。', 422)
    prior_execution = data.get('execution') or {}
    previous_volume = float(prior_execution.get('output_volume_l') or 0) if repair else 0
    if not manual_output:
        volume = qty * package.brew_volume_l
    previous_orders = env['brew.packaging.order'].sudo().search([('batch_id', '=', batch.id), ('state', 'in', ['pending_release', 'released'])])
    available = batch.actual_volume_l - sum(previous_orders.mapped('output_volume_l')) - sum(previous_orders.mapped('loss_volume_l'))
    if not math.isfinite(volume) or volume + loss > available + 1e-8:
        raise ApiError('VALIDATION_RANGE', f'本批剩余原液 {available:g} L，填写的实际产出 {volume:g} L、损耗 {loss:g} L，合计超出剩余量，请核对填写。', 422)
    if previous_volume and (abs(previous_volume - volume) > 1e-8 or abs(float(prior_execution.get('loss_volume_l') or 0) - loss) > 1e-8):
        raise ApiError('VALIDATION_RELATION', '历史包装已扣减原液，补登记的产出与损耗必须与原记录一致。', 422)
    # The plan row is already locked by task_action; stock rows are locked in FEFO order.
    lines = []
    actual = []
    planned = {str(line['material_id']): line for line in data.get('materials', [])}
    supplied = prior_execution.get('materials', []) if repair and prior_execution.get('materials') else payload.get('actual_materials') or []
    if not data.get('materials_only') and not supplied:
        supplied = [{'material_id': key, 'quantity': item['quantity']} for key, item in planned.items()]
    seen = set()
    for item in supplied:
        key = str(item.get('material_id'))
        if key not in planned or key in seen:
            raise ApiError('VALIDATION_RELATION', '请核对包装材料清单。', 422)
        seen.add(key)
        try:
            quantity = float(item.get('quantity'))
            if isinstance(item.get('quantity'), bool) or not math.isfinite(quantity) or not quantity.is_integer() or quantity < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ApiError('VALIDATION_RANGE', '实际材料用量须为非负整数。', 422)
        material = env['product.template'].sudo().search([('id', '=', int(key)), ('company_id', '=', company.id), ('brew_item_type', '=', 'packaging_material')], limit=1)
        if not material:
            raise ApiError('VALIDATION_RELATION', '包装材料不属于当前企业。', 422)
        actual.append({**planned[key], 'quantity': int(quantity)})
        remaining = quantity
        for candidate in inventory._candidate_stock(material.product_variant_id, plan.factory_id, lock=True):
            use = min(remaining, candidate['quantity'])
            if use <= 0:
                continue
            lines.append(Command.create({'product_id': material.product_variant_id.id, 'lot_id': candidate['lot'].id,
                'source_location_id': candidate['location'].id, 'uom_id': material.uom_id.id,
                'planned_qty': use, 'actual_qty': use}))
            remaining -= use
            if remaining <= 1e-8:
                break
        if remaining > 1e-8:
            raise ApiError('STOCK_INSUFFICIENT', f'{material.name}可用库存不足，还差{remaining:g}；请核对到货、质检、有效期及盘点暂停状态。', 422)
    if seen != set(planned):
        raise ApiError('VALIDATION_REQUIRED', '请填写每项包装材料的实际用量。', 422)
    session = env['brew.fermentation.session'].sudo().search([('batch_id', '=', batch.id)], limit=1)
    if not session:
        session = env['brew.fermentation.session'].sudo().create_from_batch(batch)
        batch.fermentation_session_id = session
        session.state = 'ready'
    if previous_volume:
        existing = env['brew.packaging.order'].sudo().search([('batch_id', '=', batch.id), ('state', 'in', ['pending_release', 'released'])])
        remaining = batch.actual_volume_l - sum(existing.mapped('output_volume_l')) - sum(existing.mapped('loss_volume_l')) - volume - loss
        if remaining < -1e-8 or abs(session.current_volume_l - max(0, remaining)) > 1e-8 or (remaining > 1e-8 and (session.tank_id.current_production_batch_id != batch or abs(session.tank_id.current_volume_l - remaining) > 1e-8)):
            raise ApiError('STATE_PRECONDITION_FAILED', '历史产出与实际剩余原液不一致，请管理人员先现场核对原液位置和数量。', 422)
    if session.state != 'ready' and not (previous_volume and session.state == 'completed' and session.current_volume_l == 0):
        raise ApiError('STATE_PRECONDITION_FAILED', '原液尚未准备好包装，或已全部出罐。', 422)
    order = env['brew.packaging.order'].sudo().with_context(brew_existing_packaging_output=bool(previous_volume)).create({'session_id': session.id, 'task_id': task.id,
        'product_id': package.product_id.id, 'packaging_id': package.id, 'finished_location_id': location.id,
        'planned_volume_l': session.current_volume_l + (volume + loss if previous_volume else 0),
        'material_line_ids': lines, 'note': str(payload.get('notes') or '').strip()})
    order.action_start()
    order.with_context(brew_existing_packaging_output=bool(previous_volume)).action_submit(qty, volume, loss)
    data.update(finished_packaging_id=str(package.id), finished_product_name=package.name)
    data['execution'] = {**prior_execution, 'manual_output': manual_output, 'finished_product_name': product_name if manual_output else package.name, 'output_unit': output_unit if manual_output else package.brew_barcode_level, 'actual_qty': int(qty), 'output_volume_l': volume, 'loss_volume_l': loss,
        'materials': actual, 'packaging_order_id': str(order.id), 'packaging_order_name': order.name,
        'finished_location_id': str(location.id), 'finished_location_name': location.name,
        'completed_at': prior_execution.get('completed_at') or fields.Datetime.now().isoformat(),
        'completed_by': prior_execution.get('completed_by') or user.name,
        'completed_by_id': prior_execution.get('completed_by_id') or str(user.id),
        'notes': str(payload.get('notes') or prior_execution.get('notes') or '').strip()}
    plan.with_context(brew_state_transition=True).write({'packaging_plan_json': json.dumps(data, ensure_ascii=False)})
    return data['execution']


def execution_options(env, company, plan):
    packages = env['product.packaging'].sudo().search([('product_id.product_tmpl_id', '=', plan.product_tmpl_id.id), ('company_id', 'in', [False, company.id]), ('active', '=', True), ('brew_volume_l', '>', 0)], order='sequence,id')
    locations = env['stock.location'].sudo().search([('company_id', '=', company.id), ('brew_factory_id', '=', plan.factory_id.id), ('usage', '=', 'internal'), ('brew_location_type', '=', 'finished')], order='name,id')
    tanks = env['brew.tank'].sudo().search([('factory_id', '=', plan.factory_id.id)], order='name,id')
    return {'tanks': [{'id': str(t.id), 'name': t.name, 'capacity_l': t.capacity_l, 'occupied': bool(t.current_volume_l > 0 or t.current_production_batch_id)} for t in tanks],
        'packages': [{'id': str(p.id), 'name': p.name, 'volume_l': p.brew_volume_l} for p in packages],
        'locations': [{'id': str(loc.id), 'name': loc.name} for loc in locations]}
