"""Resolve a tank's physical batch or its dispatched production assignment."""
import json
def tank_plan(env, tank):
    batch = tank.current_production_batch_id
    if not batch and tank.current_batch_no:
        batch = env['brew.production.batch'].sudo().search([
            ('company_id', '=', tank.company_id.id), ('name', '=', tank.current_batch_no)], limit=1)
    # An emptied tank must not inherit a completed historical batch's state.
    if batch and tank.current_volume_l <= 0 and batch.state in {'completed', 'cancelled'}:
        batch = env['brew.production.batch']
    plan = batch.production_plan_id if batch else env['brew.production.plan']
    if not plan and not tank.current_volume_l:
        plan = env['brew.production.plan'].sudo().search([
            ('company_id', '=', tank.company_id.id), ('tank_id', '=', tank.id),
            ('state', 'in', ['dispatched', 'in_progress'])], order='planned_start,id', limit=1)
    return plan


def tank_stage(env, tank, plan):
    labels = {'empty': '空罐', 'fermenting': '发酵中', 'conditioning': '后发酵',
              'cold_crash': '冷贮', 'ready': '待包装', 'cleaning': '清洗中',
              'maintenance': '维护中', 'abnormal': '异常'}
    if tank.status in {'cleaning', 'maintenance', 'abnormal'}:
        return tank.status, labels[tank.status]
    if plan and plan.state in {'cancelled', 'terminated'} and tank.current_volume_l > 0:
        return 'retained', '计划已关闭 · 酒液仍在罐'
    if plan and plan.state == 'done':
        packaging = json.loads(plan.packaging_plan_json or '{}')
        if not (packaging.get('materials_only') or packaging.get('material_id') or packaging.get('packaging_id') or packaging.get('execution')) and tank.current_volume_l > 0:
            return 'raw_ready', '酿造完成 · 原液在罐'
        if packaging.get('status') == 'packaged' or packaging.get('execution'):
            return 'packaged', '已包装'
    if plan and plan.state in {'dispatched', 'in_progress'}:
        tasks = env['brew.task'].sudo().search([('company_id', '=', tank.company_id.id),
            ('target_model', '=', plan._name), ('target_id', '=', plan.id),
            ('task_type', 'in', ['mashing', 'fermentation']), ('state', '!=', 'cancelled')])
        if not tasks:
            return tank.status, labels.get(tank.status, '未知状态')
        if tank.current_volume_l > 0 and tank.status in {'cold_crash', 'conditioning', 'ready'}:
            return tank.status, labels[tank.status]
        mash = tasks.filtered(lambda t: t.task_type == 'mashing')
        ferment = tasks.filtered(lambda t: t.task_type == 'fermentation')
        if tank.current_volume_l <= 0 and mash and any(t.state != 'done' for t in mash):
            return 'mashing', '糖化中' if any(t.state == 'in_progress' for t in mash) else '待糖化'
        if not ferment or any(t.state != 'done' for t in ferment):
            return 'fermentation', '发酵中' if any(t.state == 'in_progress' for t in ferment) else '待开始发酵'
        return 'finish', '待确认酿酒结束'
    return tank.status, labels.get(tank.status, '未知状态')


def task_measurements(env, plan):
    import json
    from odoo import fields
    from .fermentation_tasks import parse_time
    latest = {}
    if not plan:
        return latest
    tasks = env['brew.task'].sudo().search([('company_id', '=', plan.company_id.id),
        ('target_model', '=', plan._name), ('target_id', '=', plan.id),
        ('task_type', '=', 'fermentation'), ('state', '!=', 'cancelled')])
    records = []
    for task in tasks:
        data = json.loads(task.execution_checkpoints_json or '[]')
        if isinstance(data, dict): records.extend(data.get('records', []))
    for record in sorted(records, key=lambda row: row['measured_at']):
        if parse_time(record['measured_at']) <= fields.Datetime.now():
            for key, value in record['metrics'].items():
                latest[key] = {'value': value, 'at': record['measured_at'], 'by': record['by']}
    return latest


def tank_measurements(env, tank, plan=None):
    """One source of truth for list and detail; never display future readings."""
    from odoo import fields
    from .fermentation_tasks import parse_time
    if tank.current_volume_l <= 0:
        return {}
    latest = task_measurements(env, plan)
    domain = [('company_id', '=', tank.company_id.id), ('session_id.tank_id', '=', tank.id),
              ('session_id.state', 'not in', ['completed', 'terminated', 'transferred']),
              ('state', '=', 'approved'), ('recorded_at', '<=', fields.Datetime.now())]
    if plan:
        domain.append(('session_id.batch_id.production_plan_id', '=', plan.id))
    reading = env['brew.fermentation.reading'].sudo().search(domain, order='recorded_at desc,id desc', limit=1)
    if reading:
        for key, value in [('temperature', reading.temperature_c), ('pressure', reading.pressure_bar), ('gravity', reading.gravity_plato)]:
            previous = latest.get(key)
            if not previous or reading.recorded_at >= parse_time(previous['at']):
                latest[key] = {'value': value, 'at': reading.recorded_at.isoformat(), 'by': reading.recorded_by_id.name}
    return latest
