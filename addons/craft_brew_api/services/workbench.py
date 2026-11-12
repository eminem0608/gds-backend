"""Personal, actionable work across production and procurement.

Business documents remain the source of truth; both UI entrances use the same
mutation services. Reading this view never executes a business action.
"""
import calendar
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from odoo import fields
from ..controllers.base import ApiError
from .workflow import WorkflowService
from .task_visibility import unique_review_tasks
from .fermentation_tasks import parse_time, METRICS


class WorkbenchService:
    def __init__(self, env, user, company):
        self.env, self.user, self.company = env, user, company
        self.workflow = WorkflowService(env, user, company)
        self.zone = ZoneInfo(user.tz or 'Asia/Shanghai')
        self.now = fields.Datetime.now()
        if env.cr.dbname == 'craft_brew_dev':
            simulated = env['brew.task'].sudo().search([('company_id', '=', company.id), ('assignee_id', '=', user.id), ('task_type', '=', 'fermentation'), ('state', '=', 'in_progress')])
            offsets = [int(json.loads(t.execution_checkpoints_json or '{}').get('test_day_offset', 0)) for t in simulated]
            self.now += timedelta(days=max(offsets, default=0))
        self.today = self.local_day(self.now)
        self.tomorrow = (date.fromisoformat(self.today) + timedelta(days=1)).isoformat()
        self.items, self.records = [], []

    def local_day(self, value):
        return parse_time(value).replace(tzinfo=ZoneInfo('UTC')).astimezone(self.zone).date().isoformat()

    def _item(self, key, title, subtitle='', **data):
        return {'id': key, 'title': title, 'subtitle': subtitle, 'date': self.today, 'status': 'ready', 'button': '查看 / 处理', **data}

    def _record(self, key, title, at, subtitle='', **data):
        if at:
            self.records.append(dict(id=key, title=title, at=at, date=self.local_day(at), subtitle=subtitle, **data))

    def _material_cards(self, plan, node):
        from odoo.addons.craft_brew_workflow.models.execution_design import batch_design
        design = batch_design(json.loads(plan.recipe_snapshot_json or '{}'), plan.planned_volume_l)
        code = node['code']
        rows, group, unit = [], 'malt', 'kg'
        if code == 'mash_in':
            rows = design.get('fermentables') or []
        else:
            for prefix in ('boil_hop_', 'fwh_', 'wp_hop_'):
                if code.startswith(prefix) and code[len(prefix):].isdigit():
                    hops = design.get('hops') or []
                    index = int(code[len(prefix):])
                    rows = hops[index:index+1]
                    group, unit = 'hop', 'g'
                    break
        attributes = ['brew_brand', 'brew_product_model', 'brew_malt_class', 'brew_hop_type', 'brew_flavor_desc', 'brew_specification', 'brew_color_ebc', 'brew_extract_rate', 'brew_extract_dry_pct', 'brew_moisture_pct', 'brew_total_protein_pct', 'brew_diastatic_power_wk', 'brew_alpha_acid', 'brew_beta_acid', 'brew_cohumulone_pct', 'brew_total_oil_ml_100g']
        cards = []
        for row in rows:
            material = self.env['product.template'].sudo().browse(int(row.get('material_id') or 0)).exists()
            if material and material.company_id and material.company_id != self.company:
                continue
            details = {key: material[key] for key in attributes if material and key in material._fields}
            cards.append({'name': row.get('name') or (material.name if material else '配方原料'), 'quantity': row.get('quantity'), 'unit': unit, 'group': group, 'details': details})
        return cards

    def _production_context(self, plan, batch=None):
        return {'product_id': str(plan.product_tmpl_id.id), 'product': plan.product_tmpl_id.name, 'volume_l': plan.planned_volume_l,
                'plan_name': plan.name, 'batch_name': (batch or {}).get('name'),
                'equipment': plan.equipment_id.name or '', 'tank': plan.tank_id.name or ''}

    def _production(self, task, data):
        plan = self.env['brew.production.plan'].sudo().browse(task.target_id).exists()
        if not plan or plan.company_id != self.company:
            return
        subtitle = ' · '.join(filter(None, [plan.tank_id.name, plan.product_tmpl_id.name]))
        production = self._production_context(plan, data.get('batch'))
        base = dict(task_type=task.task_type, task_id=data['id'], version=data['version'], plan_id=str(plan.id), plan_name=plan.name, batch=data['batch'], subtitle=subtitle, production=production)
        active = task.state not in {'done', 'cancelled', 'returned'} and plan.state in {'dispatched', 'in_progress'}
        if task.task_type == 'mashing' and (task.state == 'done' or plan.state == 'done'):
            unsettled = self.env['brew.material.requisition'].sudo().search_count([('production_plan_id', '=', plan.id), ('state', '=', 'acknowledged')])
            if unsettled:
                self.items.append(self._item(f'plan:{plan.id}:settle', '登记本批原料去向', **base, kind='plan', initial_stage='material_preparation', button='登记退料与耗用'))
        if task.task_type == 'mashing':
            from odoo.addons.craft_brew_workflow.models.execution_design import batch_design, operation_highlights, ready_mash_nodes
            execution_design = batch_design(json.loads(plan.recipe_snapshot_json or '{}'), plan.planned_volume_l)
            nodes = data['execution_checkpoints']
            for node in nodes:
                if node.get('state') == 'done' and (str(node.get('completed_by_id') or '') == str(self.user.id) or (not node.get('completed_by_id') and node.get('completed_by') == self.user.name)):
                    result = node.get('value')
                    title = node['title'] + (f" · {result} {node.get('unit') or ''}" if result not in (None, '') else f" · {node['actual_quantity']:g} {node.get('unit') or ''}" if node.get('actual_quantity') is not None else '')
                    saved = node.get('execution_details') or {}
                    materials = saved.get('materials') if 'materials' in saved else self._material_cards(plan, node)
                    if node.get('actual_quantity') is not None and len(materials) == 1:
                        materials[0]['quantity'] = node['actual_quantity']
                    result_fields = []
                    if node.get('actual_quantity') is not None:
                        result_fields.append({'label': '实际数量', 'value': node['actual_quantity'], 'unit': node.get('unit') or ''})
                    if result not in (None, ''):
                        result_fields.append({'label': '实测值' if node.get('input_mode') == 'number' else '操作结果', 'value': result, 'unit': node.get('unit') or ''})
                    self._record(f"mash:{task.id}:{node['code']}", title, node.get('completed_at'), subtitle,
                        operation_title=node['title'], operator_name=node.get('completed_by') or self.user.name,
                        submitted_at=node.get('completed_at'), production={**production, **(saved.get('production') or {})}, materials=materials,
                        materials_actual=node.get('actual_quantity') is not None, highlights=result_fields,
                        requirements=saved.get('requirements') if 'requirements' in saved else operation_highlights(node, execution_design), target=node.get('target') or '',
                        note=node.get('actual_note') or '', result='已确认完成', water_adjustment=node.get('water_adjustment'), water_actual=node.get('water_actual'))
            if not active:
                return
            receipt = data.get('material_receipt') or {}
            if not receipt.get('ready'):
                self.items.append(self._item(f'mash:{task.id}:receipt', '确认本批实际领料', **base, kind='receipt', action='acknowledge-materials', receipt=receipt, status='ready' if receipt.get('can_acknowledge') else 'blocked', hint=data.get('execution_blocked_reason'), button='核对实领' if receipt.get('can_acknowledge') else '等待库管发料'))
                return
            pending = [node for node in nodes if node.get('state') != 'done']
            if not pending:
                self.items.append(self._item(f'mash:{task.id}:finish', '确认糖化完成', **base, kind='production', action='submit', button='确认完成'))
                return
            candidates = ready_mash_nodes(nodes)
            for node in candidates:
                due = None
                if node.get('timer_from'):
                    anchor = next((n for n in nodes if n['code'] == node['timer_from']), None)
                    if anchor and anchor.get('completed_at'):
                        due = parse_time(anchor['completed_at']) + timedelta(minutes=float(node.get('due_after_min') or 0))
                state = 'timer' if due and due > self.now else 'ready'
                item = self._item(f"mash:{task.id}:{node['code']}", node['title'], **base, kind='production', action='checkpoint', checkpoint_key=node['code'], input_mode=node.get('input_mode', 'confirm'), choices=node.get('choices') or [], target=node.get('target') or '', quantity=node.get('quantity'), unit=node.get('unit') or '', temperature=node.get('temperature'), due_at=due.isoformat() if due else None, status=state, button='填写实测值' if node.get('input_mode') in {'number', 'text'} else '确认操作')
                item['water_adjustment'] = node.get('water_adjustment')
                item['is_parallel'] = bool(node.get('parallel_after'))
                item['materials'] = self._material_cards(plan, node)
                item['highlights'] = operation_highlights(node, execution_design)
                if node.get('material_id'):
                    lots = []
                    for record in receipt.get('records') or []:
                        for line in record.get('lines') or []:
                            product = self.env['product.product'].sudo().browse(int(line['product']['id'])).exists()
                            if product and str(product.product_tmpl_id.id) == str(node['material_id']):
                                lots.extend({'name': a['lot']['name']} for a in line.get('allocations') or [] if a.get('lot'))
                    item['lots'] = lots
                if due:
                    item['date'] = self.local_day(due)
                self.items.append(item)
        elif task.task_type == 'fermentation':
            execution = data.get('fermentation_execution') or {}
            for record in execution.get('records') or []:
                if str(record.get('user_id')) != str(self.user.id):
                    continue
                names = []
                for key, value in (record.get('metrics') or {}).items():
                    label, unit = METRICS.get(key, (key, ''))
                    names.append(f'{label} {value:g} {unit}')
                actions = [e['action_label'] for e in execution.get('entries') or [] if e['id'] in record.get('entry_ids', []) and not e.get('metric')]
                self._record(f"ferm:{task.id}:{record['id']}", '；'.join(actions + names) or '发酵操作记录', record['measured_at'], subtitle, note=record.get('note'), backfill=record.get('backfill'), operator_name=record.get('by') or self.user.name, submitted_at=record.get('submitted_at'), production=production, operation_title='；'.join(actions) or '发酵测量记录', highlights=[{'label': METRICS.get(key,(key,''))[0], 'value': value, 'unit': METRICS.get(key,(key,''))[1]} for key,value in (record.get('metrics') or {}).items()], result='已记录')
            if not active or data.get('execution_blocked_reason'):
                return
            if not execution.get('started_at'):
                self.items.append(self._item(f'ferm:{task.id}:start', '确认进罐并开始发酵', **base, kind='production', action='start', button='确认开始'))
                return
            for entry in execution.get('entries') or []:
                if entry['status'] == 'done':
                    continue
                if entry['rule'].get('condition') != 'time':
                    rule = entry['rule']
                    label, unit = METRICS.get(rule.get('condition'), (rule.get('condition'), ''))
                    latest = next((r['metrics'][rule['condition']] for r in sorted(execution.get('records') or [], key=lambda r: (r['measured_at'], r['submitted_at']), reverse=True) if rule['condition'] in r.get('metrics', {})), None)
                    condition = f"{label} {rule.get('operator', '')} {rule.get('condition_value', '')} {unit}"
                    current = f"当前：{latest:g} {unit}" if latest is not None else '尚未录入相关测量数据'
                    self.items.append(self._item(f"ferm:{task.id}:{entry['id']}", entry['action_label'], **base, kind='fermentation_condition', entry_id=entry['id'], metric=entry.get('metric'), condition_metric=rule['condition'], operation_target=rule.get('target'), test_available=self.env.cr.dbname == 'craft_brew_dev', status='condition', condition_ready=entry['status'] != 'waiting', hint=f"触发条件：{condition} · {current}", button='查看 / 记录'))
                    continue
                metric = entry.get('metric')
                label, unit = METRICS.get(metric, ('', ''))
                highlights = [{'label': '操作目标', 'value': entry['rule']['target'], 'unit': '℃' if entry['rule']['action'] == 'cool_to' else 'bar' if entry['rule']['action'] == 'increase_pressure' else ''}] if entry['rule'].get('target') else []
                self.items.append(self._item(f"ferm:{task.id}:{entry['id']}", entry['action_label'], **base, kind='fermentation', action='fermentation-record', entry_id=entry['id'], metric=metric, unit=unit, target=entry['rule'].get('target') or '', scheduled_date=entry['date'], highlights=highlights, metric_label=label, input_mode='number' if metric else 'confirm', button='填写测量值' if metric else '记录操作', **{'date': entry['date']}))
            if execution.get('can_complete'):
                self.items.append(self._item(f'ferm:{task.id}:finish', '确认发酵结束', **base, kind='plan', initial_stage='fermentation', button='核对并结束'))

    def _business(self):
        roles = set(self.user.brew_role_codes())
        admin = self.user.has_group('craft_brew_core.group_brew_admin')
        warehouse, purchaser = admin or 'warehouse_keeper' in roles, admin or 'purchaser' in roles
        if not (warehouse or purchaser or 'production_director' in roles):
            return
        from .purchase import PurchaseService
        from .inventory import InventoryService
        purchase = PurchaseService(self.env, self.user, self.company)
        factory_ids = purchase._factory_ids()
        scope = [('company_id', '=', self.company.id), ('factory_id', 'in', factory_ids)]

        def add(kind, action, record, title, subtitle, button='查看 / 处理', **extra):
            self.items.append(self._item(f'{kind}:{record.id}:{action}', title, subtitle, kind='inventory', operation=dict(kind=kind, action=action, id=str(record.id)), button=button, **extra))

        orders = self.env['purchase.order'].sudo().search([('company_id', '=', self.company.id), ('brew_factory_id', 'in', factory_ids), ('state', 'in', ['draft', 'sent', 'purchase'])])
        for record in orders:
            data = purchase._serialize_order(record)
            subtitle = record.partner_id.name
            if purchaser and (admin or not record.user_id or record.user_id == self.user):
                if record.state in {'draft', 'sent'}:
                    add('order', 'place', record, '核对采购单并下单', subtitle, '核对并下单', quantity=record.amount_total, unit=record.currency_id.name)
            if warehouse and data['can_receive'] and not self.env['brew.receipt'].sudo().search_count([('purchase_order_id', '=', record.id), ('state', '=', 'draft')]):
                add('order', 'receive', record, '登记实际收货', subtitle, '核对并收货')
        if purchaser:
            for record in self.env['brew.purchase.requisition'].sudo().search(scope + [('state', '=', 'approved')]):
                # ordered_qty includes allocations to active draft orders in the business model.
                if any(line.approved_qty > line.ordered_qty + 1e-8 for line in record.line_ids):
                    add('requisition', 'order', record, '从需求生成采购单', record.name, '生成采购单', hint=f'{len(record.line_ids)} 项采购需求')
        for record in self.env['brew.purchase.requisition'].sudo().search(scope + [('requester_id', '=', self.user.id), ('state', 'in', ['draft', 'returned'])]):
            add('requisition', 'edit', record, '完善采购需求' if record.state == 'draft' else '修改退回的采购需求', record.name, '编辑并提交', hint=record.return_reason or '')
        if not warehouse:
            return
        for record in self.env['brew.receipt'].sudo().search(scope + [('state', '=', 'draft')]):
            add('receipt', 'edit', record, '继续登记收货', record.purchase_order_id.partner_id.name, '继续收货')
        for record in self.env['brew.quality.inspection'].sudo().search(scope + [('state', 'in', ['draft', 'returned', 'submitted']), ('receipt_id.state', '=', 'arrived')]):
            add('inspection', 'quality', record, '质检并入库', record.receipt_id.purchase_order_id.partner_id.name, '录入质检结果', hint=f'{len(record.line_ids)} 项待检物料')
        for record in self.env['brew.purchase.return'].sudo().search(scope + [('state', 'in', ['draft', 'returned', 'approved'])]):
            action = 'execute' if record.state == 'approved' else 'edit'
            add('return', action, record, '办理退货出库' if action == 'execute' else '完善采购退货单', record.name, '执行退货' if action == 'execute' else '编辑并提交')
        inventory = InventoryService(self.env, self.user, self.company)
        for record in self.env['brew.inventory.count'].sudo().search(scope + ['|', ('state', 'in', ['draft', 'counting']), '&', ('state', '=', 'pending_approval'), ('approval_instance_id.state', 'in', ['approved', 'returned'])]):
            add('count', 'open', record, {'draft': '开始库存盘点', 'counting': '录入盘点实数', 'pending_approval': '核对盘点差异'}[record.state], record.name, '查看盘点任务')
        for record in self.env['brew.inventory.transfer'].sudo().search(scope + [('state', '=', 'draft')]):
            self.items.append(self._item(f'transfer:{record.id}', '确认库存调拨', record.name, kind='stock_action', operation=dict(kind='transfer', id=str(record.id)), document=inventory._serialize_transfer(record), button='核对调拨'))
        for record in self.env['brew.inventory.scrap'].sudo().search(scope + ['|', ('state', '=', 'approved'), '&', ('state', '=', 'pending_approval'), ('approval_instance_id.state', '=', 'approved')]):
            self.items.append(self._item(f'scrap:{record.id}', '处理库存报损', record.name, kind='stock_action', operation=dict(kind='scrap', id=str(record.id)), document=inventory._serialize_scrap(record), button='核对报损'))

    def _audit_records(self, first, end):
        from .history_policy import is_task_result
        labels = {
            'purchase_requisition': '采购需求', 'purchase_order': '采购单', 'purchase_receipt': '收货',
            'quality_inspection': '质检', 'purchase_return': '退货', 'inventory_count': '盘点',
            'inventory_transfer': '调拨', 'inventory_scrap': '报损', 'approval': '审批', 'material_requisition': '领料',
        }
        actions = {'create': '创建', 'create_from_requisitions': '生成采购单', 'update': '修改', 'submit': '提交', 'place': '正式下单', 'confirm-arrival': '确认实际收货', 'put-away': '入库', 'complete': '质检并入库', 'update_results': '登记质检结果', 'execute': '执行退货', 'start': '开始', 'update_lines': '录入实数', 'confirm': '确认执行', 'approve': '审批通过', 'return': '退回', 'issue': '确认发料', 'settle': '结清物料', 'undo-issue': '收回重发', 'recommend-lots': '生成拣料清单', 'acknowledge': '确认实领'}
        lower = datetime.combine(date.fromisoformat(first), datetime.min.time(), self.zone).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
        upper = datetime.combine(date.fromisoformat(end), datetime.min.time(), self.zone).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
        logs = self.env['brew.audit.log'].sudo().search([('company_id', '=', self.company.id), ('user_id', '=', self.user.id), ('event_type', 'in', list(labels)), ('create_date', '>=', lower), ('create_date', '<', upper)], order='create_date desc,id desc')
        for log in logs:
            if not is_task_result(log.event_type, log.action):
                continue
            summary = json.loads(log.summary_json or '{}')
            model_name = {'purchase_requisition':'brew.purchase.requisition','purchase_order':'purchase.order','purchase_receipt':'brew.receipt','quality_inspection':'brew.quality.inspection','purchase_return':'brew.purchase.return','inventory_count':'brew.inventory.count','inventory_transfer':'brew.inventory.transfer','inventory_scrap':'brew.inventory.scrap','material_requisition':'brew.material.requisition'}.get(log.event_type)
            reference = ''
            document = None
            instance = None
            if log.event_type == 'approval' and summary.get('instance_id'):
                instance = self.env['brew.approval.instance'].sudo().browse(int(summary['instance_id'])).exists()
                if instance and instance.company_id == self.company:
                    supported = {'brew.purchase.requisition','purchase.order','brew.receipt','brew.quality.inspection','brew.purchase.return','brew.inventory.count','brew.inventory.transfer','brew.inventory.scrap','brew.material.requisition','brew.production.plan'}
                    if instance.target_model in supported:
                        model_name = instance.target_model
                        summary = {**summary, 'id': instance.target_id}
            if model_name and summary.get('id'):
                source = self.env[model_name].sudo().browse(int(summary['id'])).exists()
                allowed = source and source.company_id == self.company
                if allowed:
                    factory = source.brew_factory_id if model_name == 'purchase.order' else source.factory_id
                    allowed = factory in self.user.brew_allowed_factories(self.company) if factory else self.user.brew_has_headquarters_scope(self.company)
                if allowed:
                    reference = source.name
                    from .record_document import document_details
                    document = document_details(source, instance)
            self._record(f'audit:{log.id}', f'{labels[log.event_type]} · {actions[log.action]}', log.create_date.isoformat(), reference, operator_name=self.user.name, submitted_at=log.create_date.isoformat(), operation_title=f'{labels[log.event_type]} · {actions[log.action]}', result=actions[log.action], business_record=True, document=document)

    def get(self, selected=None, month=None, include_history=True):
        try:
            selected_date = date.fromisoformat(selected or self.today)
            first = date.fromisoformat((month or selected_date.strftime('%Y-%m')) + '-01')
        except (ValueError, TypeError):
            raise ApiError('VALIDATION_FORMAT', '请选择有效日期或月份。', 422)
        if selected_date.isoformat() > self.today:
            selected_date = date.fromisoformat(self.today)
        end = first + timedelta(days=calendar.monthrange(first.year, first.month)[1])
        # Do not truncate personal work to the first API page.
        self.workflow._ensure_returned_recipe_tasks()
        roles = list(self.workflow._role_codes())
        factory_ids = self.user.brew_allowed_factories(self.company).ids
        domain = [('company_id', '=', self.company.id), ('target_model', '!=', 'seed.dev.data'), '|', ('assignee_id', '=', self.user.id), '&', ('assignee_id', '=', False), ('role_code', 'in', roles)]
        tasks = self.env['brew.task'].sudo().search(domain, order='priority desc,id')
        for task in unique_review_tasks(tasks, self.user.id):
            if not self.workflow.record_in_scope(task):
                continue
            data = self.workflow.serialize_task(task)
            if task.target_model == 'brew.production.plan' and task.task_type in {'mashing', 'fermentation'}:
                if task.assignee_id == self.user:
                    self._production(task, data)
            elif task.state not in {'done', 'cancelled', 'returned'}:
                if task.task_type in {'material_preparation', 'packaging'}:
                    if not data.get('plan') or data['plan']['state'] not in {'dispatched', 'in_progress', 'done'}:
                        continue
                    self.items.append(self._item(f"plan:{task.id}", '库管拣料并发到领料库' if task.task_type == 'material_preparation' else '执行包装', data['plan']['product'], kind='plan', task_id=data['id'], plan_id=str(task.target_id), initial_stage=task.task_type, production=self._production_context(self.env['brew.production.plan'].sudo().browse(task.target_id), data.get('batch')), button='核对并发料' if task.task_type == 'material_preparation' else '查看 / 执行'))
                else:
                    self.items.append(self._item(f'task:{task.id}', data['name'], kind='legacy', legacy_task=data, button='查看 / 处理', **({'date': self.local_day(task.due_at), 'due_at': task.due_at.isoformat()} if task.due_at else {})))
        approvals, _total = self.workflow.list_approvals(1, 100000, state='pending')
        for approval in approvals:
            step = self.env['brew.approval.step'].sudo().browse(int(approval['id']))
            if not self.workflow.record_in_scope(step.instance_id):
                continue
            if approval.get('state') == 'pending' and approval.get('is_current', False):
                self.items.append(self._item('approval:' + approval['id'], '审批 · ' + approval.get('name', ''), kind='approval', approval=approval, button='查看审批'))
        self._business()
        if include_history:
            self._audit_records(first.isoformat(), end.isoformat())
        unique = {item['id']: item for item in self.items}
        items = list(unique.values())
        today_items = [i for i in items if i['date'] <= self.today and i['status'] != 'timer']
        today_items.sort(key=lambda i: (i['status'] == 'blocked', bool(i.get('is_parallel')), not bool(i.get('due_at')), i.get('due_at') or '', i['id']))
        counts = {}
        records = {r['id']: r for r in self.records}.values()
        for record in records:
            if first.isoformat() <= record['date'] < end.isoformat():
                counts[record['date']] = counts.get(record['date'], 0) + 1
        def product_key(item):
            production = item.get('production') or {}
            return str(production.get('product_id') or ('name:' + production['product'] if production.get('product') else '__other'))

        def product_groups(source, counted):
            groups = {}
            for item in source:
                key = product_key(item)
                groups.setdefault(key, {'id': key, 'name': (item.get('production') or {}).get('product') or '其他事项', 'count': 0})
            for item in counted:
                key = product_key(item)
                if key in groups:
                    groups[key]['count'] += 1
            return sorted(groups.values(), key=lambda g: (g['id'] == '__other', g['name'], g['id']))

        pending = [i for i in today_items if not i['id'].endswith(':monitor')] + [i for i in items if i['status'] == 'timer']
        daily_records = [r for r in records if r['date'] == selected_date.isoformat()]
        monthly_records = [r for r in records if first.isoformat() <= r['date'] < end.isoformat()]
        today_products = product_groups(pending + [i for i in items if i['date'] == self.tomorrow] + [r for r in records if r['date'] == self.today], pending)
        history_products = product_groups(monthly_records, daily_records)
        product_calendar = {}
        for record in monthly_records:
            key = product_key(record)
            per_day = product_calendar.setdefault(key, {})
            per_day[record['date']] = per_day.get(record['date'], 0) + 1
        def batch_key(item):
            production = item.get('production') or {}
            if production.get('plan_name'):
                return 'plan:' + production['plan_name']
            if production.get('batch_name'):
                return 'batch:' + production['batch_name']
            return '__other'

        def batch_groups(source, counted):
            groups = {}
            for item in source:
                key = batch_key(item)
                production = item.get('production') or {}
                groups.setdefault(key, {'id': key, 'name': production.get('batch_name') or production.get('plan_name') or '其他事项', 'product_id': product_key(item), 'production': production, 'count': 0})
            for item in counted:
                key = batch_key(item)
                if key in groups:
                    groups[key]['count'] += 1
            return list(groups.values())
        today_batches = batch_groups(pending + [i for i in items if i['date'] == self.tomorrow] + [r for r in records if r['date'] == self.today], pending)
        history_batches = batch_groups(records, daily_records)
        plan_names = [b['production'].get('plan_name') for b in history_batches if b['production'].get('plan_name')]
        plans_by_name = {p.name: p for p in self.env['brew.production.plan'].sudo().search([('company_id', '=', self.company.id), ('name', 'in', plan_names)])}
        for batch in history_batches:
            plan = plans_by_name.get(batch['production'].get('plan_name'))
            batch['completed'] = bool(plan and plan.state in {'done', 'cancelled', 'terminated'})
        batch_calendar = {}
        for record in monthly_records:
            per_day = batch_calendar.setdefault(batch_key(record), {})
            per_day[record['date']] = per_day.get(record['date'], 0) + 1
        test_fermentations = []
        if self.env.cr.dbname == 'craft_brew_dev':
            for task in tasks:
                if task.target_model != 'brew.production.plan' or task.task_type != 'fermentation' or task.state != 'in_progress' or task.assignee_id != self.user:
                    continue
                plan = self.env['brew.production.plan'].sudo().browse(task.target_id).exists()
                if not plan or plan.company_id != self.company or plan.state not in {'dispatched', 'in_progress'}:
                    continue
                execution = self.workflow.fermentation_execution(task)
                if not execution.get('started_at'):
                    continue
                batch = self.env['brew.production.batch'].sudo().search([('production_plan_id', '=', plan.id)], limit=1)
                test_fermentations.append({'id': str(task.id), 'version': self.workflow.serialize_task(task)['version'], 'production': self._production_context(plan, {'name': batch.name} if batch else None)})
        return dict(test_fermentations=test_fermentations, today_batches=today_batches, history_batches=history_batches, batch_calendar=batch_calendar, today_products=today_products, history_products=history_products, product_calendar=product_calendar, today=self.today, now=self.now.isoformat(), timezone=str(self.zone), selected_date=selected_date.isoformat(), month=first.strftime('%Y-%m'), items=today_items, pending_count=len([i for i in today_items if not i['id'].endswith(':monitor')])+len([i for i in items if i['status'] == 'timer']), timers=[i for i in items if i['status'] == 'timer'], tomorrow=[i for i in items if i['date'] == self.tomorrow], calendar=counts, records=sorted([r for r in records if r['date'] == selected_date.isoformat()], key=lambda r: r['at'], reverse=True))
