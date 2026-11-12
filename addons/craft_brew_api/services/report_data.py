"""Scoped, Chinese-labelled production and inventory reports."""
import csv
import io
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from odoo import fields
from ..controllers.base import ApiError
from .reporting import ReportingService
from .fermentation_tasks import METRICS, ACTIONS, parse_time

STATES = {'draft':'草稿','submitted':'待审核','approved':'已审核','returned':'已退回','superseded':'已更正',
          'pending_approval':'待审批','counting':'盘点中','done':'已完成','cancelled':'已取消','terminated':'已终止','dispatched':'已下发','in_progress':'执行中','stocking':'备货中',
          'active':'预占中','released':'已释放','consumed':'已耗用','expired':'已过期',
          'qualified':'合格','pending':'待检','rejected':'不合格','purchase':'已下单','cancel':'已取消','sent':'已发送'}
REASONS = {'damage':'破损','expired':'过期报废','measurement':'计量误差','missing_transaction':'单据未及时入账',
           'unrecorded_issue_return':'领料或退料未登记','other':'其他'}
LABELS = {'temperature':'温度','pressure':'压力','gravity':'糖度','ph':'酸碱度','volume':'体积',
          'temperature_c':'温度','pressure_bar':'压力','gravity_plato':'糖度','volume_l':'体积',
          'duration_min':'时长','actual_volume_l':'实际体积','amount_total':'总金额','order_date':'订单日期',
          'status':'状态','mash_ph':'糖化酸碱度','original_gravity':'原始糖度','target_temperature':'目标温度',
          'actual_temperature':'实测温度','duration':'时长','water_volume_l':'用水量'}
COMMON = [('factory','酒厂'),('product','酒品'),('batch','批次 / 计划')]
def columns(items):
    return [{'key':key,'label':label} for key,label in items]

REPORTS = [
 {'code':'production','group':'生产记录','name':'生产批次与产量','description':'逐批核对计划产量、实际产量、完成时间和酒液去向','dated':True,
  'columns':columns([('at','计划开始时间'),('completed_at','酿造完成时间')]+COMMON+[('tank','罐位'),('planned','计划产量（L）'),('actual','实际产量（L）'),('loss','登记损耗（L）'),('disposition','酒液去向'),('state','计划状态')])},
 {'code':'liquid','group':'生产记录','name':'在罐原液','description':'当前仍在生产罐内的原液，含估算量与实测量口径','dated':False,
  'columns':columns([('factory','酒厂'),('product','酒品'),('batch','生产批次'),('reference','生产计划'),('tank','罐位'),('quantity','在罐体积'),('unit','单位'),('basis','数量口径'),('state','生产状态')])},
 {'code':'mash','group':'生产记录','name':'糖化记录','description':'投料、保温、过滤、煮沸与冷却的现场记录','dated':True,
  'columns':columns([('at','记录时间')]+COMMON+[('operation','工艺节点'),('result','实测 / 操作结果'),('operator','操作人'),('state','记录状态')])},
 {'code':'fermentation','group':'生产记录','name':'发酵记录','description':'发酵测量、条件操作和补录记录','dated':True,
  'columns':columns([('at','测量时间')]+COMMON+[('tank','罐体'),('temperature','温度（℃）'),('gravity','糖度（°P）'),('pressure','压力（巴）'),('attenuation','实际发酵度（%）'),('operation','操作内容'),('operator','操作人'),('state','记录状态')])},
 {'code':'balance','group':'库存报表','name':'库存结存','description':'按物料、批次和库位核对当前账面库存','dated':False,
  'columns':columns([('factory','酒厂'),('product','物料 / 酒品'),('lot','库存批次'),('location','库位'),('quantity','账面数量'),('unit','单位'),('reserved','标准库存预留'),('state','质量状态')])},
 {'code':'movements','group':'库存报表','name':'出入库流水','description':'追溯入库、领料、出库、调拨及盘点调整','dated':True,
  'columns':columns([('at','业务时间'),('factory','酒厂'),('product','物料 / 酒品'),('lot','批次'),('quantity','数量'),('unit','单位'),('source_location','来源库位'),('destination_location','目标库位'),('reference','关联单据')])},
 {'code':'expiry','group':'库存报表','name':'批次效期','description':'查看库存批次的到期日期和临期风险','dated':False,
  'columns':columns([('factory','酒厂'),('product','物料 / 酒品'),('lot','批次'),('location','库位'),('quantity','账面数量'),('unit','单位'),('expiry','到期日期'),('days','剩余天数'),('state','效期状态')])},
 {'code':'reservations','group':'库存报表','name':'库存预占','description':'核对生产需求占用的物料和库存批次','dated':True,
  'columns':columns([('at','预占时间'),('factory','酒厂'),('product','物料'),('lot','批次'),('location','库位'),('quantity','预占数量'),('unit','单位'),('state','状态'),('expires','预占到期时间')])},
 {'code':'counts','group':'库存报表','name':'盘点差异','description':'账面、实盘、盘盈盘亏和差异原因','dated':True,
  'columns':columns([('at','盘点创建时间'),('reference','盘点单'),('factory','酒厂'),('product','物料 / 酒品'),('lot','批次'),('location','库位'),('book','账面数量'),('actual','实盘数量'),('difference','差异数量'),('unit','单位'),('reason','差异原因'),('note','差异说明'),('state','单据状态')])},
 {'code':'consumption','group':'库存报表','name':'生产耗用','description':'按生产批次追溯原料实际耗用和批号','dated':True,
  'columns':columns([('at','登记时间')]+COMMON+[('material','耗用物料'),('lot','原料批次'),('quantity','耗用数量'),('unit','单位'),('operation','用途')])},
 {'code':'purchase','group':'采购报表','name':'采购订单','description':'供应商、订单日期、总金额和采购进度','dated':True,
  'columns':columns([('at','订单日期'),('reference','采购订单'),('factory','酒厂'),('supplier','供应商'),('amount','总金额'),('currency','币种'),('state','状态')])},
]

class ReportDataService(ReportingService):
    def _role(self, *names):
        return any(self.user.has_group('craft_brew_core.group_brew_' + name) for name in names)

    def catalog(self):
        production = self._role('admin','general_manager','production_director','brewery_manager')
        inventory = production or self._role('warehouse','purchase','finance')
        purchase = self._role('admin','general_manager','purchase','finance','brewery_manager')
        reports = [r for r in REPORTS if {'生产记录':production,'库存报表':inventory,'采购报表':purchase}[r['group']]]
        return {'timezone':self.user.tz or 'Asia/Shanghai', 'scope_kind':'headquarters' if self.user.brew_has_headquarters_scope(self.company) else 'factory', 'scope_label':'授权酒厂' if self.user.brew_has_headquarters_scope(self.company) else '本厂范围', 'items':reports, 'factories':[{'id':str(f.id),'name':f.name} for f in self.env['brew.factory'].sudo().browse(self._factory_ids())]}

    def _definition(self, code):
        definition = next((r for r in self.catalog()['items'] if r['code'] == code), None)
        if not definition:
            raise ApiError('PERMISSION_DENIED','当前账号无权查看该报表。',403)
        return definition

    def _bounds(self, filters):
        zone = ZoneInfo(self.user.tz or 'Asia/Shanghai')
        values = []
        for key in ['date_from','date_to']:
            try:
                day = fields.Date.to_date(filters[key]) if filters.get(key) else None
                values.append(datetime.combine(day, time.min).replace(tzinfo=zone).astimezone(ZoneInfo('UTC')).replace(tzinfo=None) if day else None)
            except (ValueError, TypeError):
                raise ApiError('VALIDATION_FORMAT','请选择有效的起止日期。',422)
        first, last = values
        if first and last and first > last:
            raise ApiError('VALIDATION_RANGE','开始日期不能晚于结束日期。',422)
        return first, last + timedelta(days=1) if last else None

    def _scope(self, factories, field='factory_id'):
        return [('company_id','=',self.company.id),(field,'in',factories)]

    def _production(self, code, factories):
        task_type = 'mashing' if code == 'mash' else 'fermentation'
        tasks = self.env['brew.task'].sudo().search(self._scope(factories)+[
            ('target_model','=','brew.production.plan'),('task_type','=',task_type)])
        plans = {p.id:p for p in self.env['brew.production.plan'].sudo().browse(tasks.mapped('target_id')).exists()
                 if p.company_id == self.company and p.factory_id.id in factories}
        batches = {b.production_plan_id.id:b for b in self.env['brew.production.batch'].sudo().search(self._scope(factories))}
        rows = []
        for task in tasks:
            plan = plans.get(task.target_id)
            if not plan: continue
            batch = batches.get(plan.id)
            common = {'factory':plan.factory_id.name,'product':plan.product_tmpl_id.name,'batch':batch.name if batch else plan.name,
                      'plan_id':str(plan.id),'tank':plan.tank_id.name,'reference':plan.name}
            raw = json.loads(task.execution_checkpoints_json or ('[]' if code=='mash' else '{}'))
            if code == 'mash':
                for i, node in enumerate(raw if isinstance(raw,list) else []):
                    if node.get('state') != 'done' or not node.get('completed_at'): continue
                    value = node.get('value')
                    if value is None or value == '': value = node.get('actual_quantity')
                    if isinstance(value, bool): value = '是' if value else '否'
                    rows.append({**common,'id':f'task-{task.id}-{i}','at':node['completed_at'],'operation':node.get('title') or '糖化操作',
                                 'result':f"{value} {node.get('unit') or ''}".strip() if value is not None and value != '' else '已确认',
                                 'operator':node.get('completed_by') or '未记录','state':'已记录','note':node.get('note') or '',
                                 'details':self._node_details(node)})
            elif isinstance(raw,dict):
                entries = {str(e['id']):e for e in raw.get('entries',[])}
                for i, record in enumerate(raw.get('records',[])):
                    metrics = record.get('metrics') or {}
                    actions = [ACTIONS.get((entries.get(str(e),{}).get('rule') or {}).get('action'),'工艺操作') for e in record.get('entry_ids',[])]
                    rows.append({**common,'id':f'task-{task.id}-{i}','at':record.get('measured_at'),**metrics,
                                 'operator':record.get('by') or '未记录','operation':'、'.join(dict.fromkeys(actions)) or '发酵测量',
                                 'state':'补录' if record.get('backfill') else '已记录','note':record.get('note') or '',
                                 'details':[{'label':METRICS[k][0],'value':v,'unit':METRICS[k][1]} for k,v in metrics.items() if k in METRICS]})
        if code == 'mash':
            records = self.env['brew.production.record'].sudo().search(self._scope(factories)+[('record_type','in',['cleaning','mashing','lautering','boiling','cooling','other'])])
            types = dict(records._fields['record_type'].selection)
            for r in records:
                params = json.loads(r.parameters_json or '{}')
                details = [{'label':LABELS.get(k,'其他工艺参数'),'value':self._plain(v)} for k,v in params.items()]
                rows.append({'id':f'record-{r.id}','at':r.recorded_at.isoformat(),'factory':r.factory_id.name,'product':r.batch_id.product_tmpl_id.name,
                             'batch':r.batch_id.name,'plan_id':str(r.batch_id.production_plan_id.id),'operation':types.get(r.record_type,'工艺记录'),
                             'result':'；'.join(f"{d['label']}：{d['value']}" for d in details) or '已记录',
                             'operator':r.operator_id.name,'state':STATES.get(r.state,'待核对'),'note':r.note or '', 'details':details})
        else:
            readings = self.env['brew.fermentation.reading'].sudo().search(self._scope(factories))
            for r in readings:
                rows.append({'id':f'reading-{r.id}','at':r.recorded_at.isoformat(),'factory':r.factory_id.name,'product':r.session_id.batch_id.product_tmpl_id.name,
                             'batch':r.session_id.batch_id.name,'plan_id':str(r.session_id.batch_id.production_plan_id.id),'tank':r.session_id.tank_id.name,
                             'temperature':r.temperature_c,'gravity':r.gravity_plato,'pressure':r.pressure_bar,'operation':'发酵测量',
                             'operator':r.recorded_by_id.name,'state':STATES.get(r.state,'待核对'),'note':r.note or '',
                             'details':[{'label':'酸碱度','value':r.ph},{'label':'酒液体积','value':r.volume_l,'unit':'升'}]})
        return rows

    @staticmethod
    def _plain(value):
        if isinstance(value,bool): return '是' if value else '否'
        if isinstance(value,(dict,list)): return '见关联批次详情'
        if isinstance(value,str) and value in STATES: return STATES[value]
        return value

    def _node_details(self,node):
        details = []
        for key,label in [('target','工艺目标'),('actual_quantity','实际数量'),('completed_by','操作人')]:
            if node.get(key) is not None and node.get(key) != '': details.append({'label':label,'value':self._plain(node[key])})
        return details

    @staticmethod
    def _location_name(location):
        translations = {'Stock':'主库存区', 'Input':'收货区', 'Output':'出库区',
                        'Production':'生产区', 'Inventory adjustment':'库存调整',
                        'Customers':'客户', 'Vendors':'供应商', 'Scrap':'报废区',
                        'Quality Control':'质检区', 'Packing Zone':'包装区'}
        name = location.with_context(lang='en_US').name or ''
        return translations.get(name, location.name or '未设置')

    def _inventory(self,code,factories):
        locations = self.env['stock.location'].sudo().search([('brew_factory_id','in',factories),('usage','=','internal'),('company_id','in',[False,self.company.id])])
        ids = locations.ids
        rows = []
        if code in {'balance','expiry'}:
            quants = self.env['stock.quant'].sudo().search([('company_id','=',self.company.id),('location_id','in',ids),('quantity','!=',0)])
            today = fields.Date.context_today(self.user.with_context(tz=self.user.tz or 'Asia/Shanghai'))
            for q in quants:
                expiry = q.lot_id.brew_expiration_date
                days = (expiry-today).days if expiry else None
                rows.append({'id':str(q.id),'factory':q.location_id.brew_factory_id.name,'product':q.product_id.display_name,
                             'lot':q.lot_id.name or '未分批次','location':self._location_name(q.location_id),'quantity':q.quantity,'unit':q.product_id.uom_id.name,
                             'reserved':q.reserved_quantity,'expiry':expiry.isoformat() if expiry else None,'days':days,
                             'state':('未设置效期' if days is None else '已过期' if days<0 else '临期（30天内）' if days<=30 else '效期正常') if code=='expiry' else STATES.get(q.lot_id.brew_quality_status,'未设置')})
        elif code=='movements':
            moves = self.env['stock.move.line'].sudo().search([('company_id','=',self.company.id),('state','=','done'),'|',('location_id','in',ids),('location_dest_id','in',ids)])
            for m in moves:
                location = m.location_id if m.location_id.id in ids else m.location_dest_id
                rows.append({'id':str(m.id),'at':m.date.isoformat(),'factory':location.brew_factory_id.name,'product':m.product_id.display_name,
                             'lot':m.lot_id.name or '未分批次','quantity':m.quantity,'unit':m.product_uom_id.name,'source_location':self._location_name(m.location_id),
                             'destination_location':self._location_name(m.location_dest_id),'reference':m.move_id.brew_source_reference or m.move_id.reference})
        elif code=='reservations':
            records = self.env['brew.inventory.reservation'].sudo().search([('company_id','=',self.company.id),('location_id','in',ids)])
            for r in records:
                rows.append({'id':str(r.id),'at':r.create_date.isoformat(),'factory':r.location_id.brew_factory_id.name,'product':r.product_id.display_name,
                             'lot':r.lot_id.name or '未分批次','location':self._location_name(r.location_id),'quantity':r.quantity,'unit':r.uom_id.name,
                             'state':STATES.get(r.state,'待核对'),'expires':r.expires_at.isoformat() if r.expires_at else None})
        elif code=='counts':
            records = self.env['brew.inventory.count.line'].sudo().search([('company_id','=',self.company.id),('location_id','in',ids)])
            for r in records:
                rows.append({'id':str(r.id),'at':r.count_id.create_date.isoformat(),'reference':r.count_id.name,'factory':r.location_id.brew_factory_id.name,
                             'product':r.product_id.display_name,'lot':r.lot_id.name or '未分批次','location':self._location_name(r.location_id),'book':r.theoretical_qty,
                             'actual':r.counted_qty if r.is_counted else None,'difference':r.difference_qty if r.is_counted else None,'unit':r.uom_id.name,'reason':REASONS.get(r.difference_reason,'账实一致' if r.is_counted else '未盘'),
                             'state':STATES.get(r.count_id.state,'待核对'),'note':r.difference_note or ''})
        elif code=='consumption':
            records = self.env['brew.production.material.actual'].sudo().search([('company_id','=',self.company.id),('batch_id.factory_id','in',factories)])
            types = dict(records._fields['action_type'].selection)
            for r in records:
                rows.append({'id':str(r.id),'at':r.create_date.isoformat(),'factory':r.batch_id.factory_id.name,'product':r.batch_id.product_tmpl_id.name,
                             'batch':r.batch_id.name,'material':r.product_id.display_name,'lot':r.lot_id.name,'quantity':r.quantity,'unit':r.uom_id.name,
                             'operation':types.get(r.action_type,'生产耗用'),'plan_id':str(r.batch_id.production_plan_id.id)})
        return rows

    def data(self,code,filters,page=1,size=20,export=False):
        definition = self._definition(code)
        factories = self._factory_ids(filters.get('factory_id'))
        first,last = self._bounds(filters)
        if code == 'production':
            records = self.env['brew.production.plan'].sudo().search(self._scope(factories))
            rows = []
            for r in records:
                completion = json.loads(r.brewing_completion_json or '{}')
                rows.append({'id':str(r.id), 'at':r.planned_start.isoformat(), 'completed_at':completion.get('confirmed_at'),
                    'factory':r.factory_id.name, 'product':r.product_tmpl_id.name, 'batch':r.name, 'tank':r.tank_id.name,
                    'planned':r.planned_volume_l, 'actual':completion.get('actual_volume_l'), 'loss':completion.get('loss_volume_l'),
                    'disposition':{'in_tank':'仍在罐内','emptied':'已出酒'}.get(completion.get('disposition'),'尚未确认'),
                    'state':STATES.get(r.state,r.state) + ('（已删除）' if r.deleted_at else '')})
        elif code == 'liquid':
            from .stock_browser import StockBrowserService
            service = StockBrowserService(self.env,self.user,self.company,enforce_access=False)
            rows = []
            for factory in factories:
                for r in service.production_liquids({'factory_id':factory}):
                    rows.append({'id':r['batch_id'], 'factory':r['factory']['name'], 'product':r['product']['name'],
                        'batch':r['batch_name'], 'reference':r['plan_name'], 'tank':r['tank'], 'quantity':r['volume_l'], 'unit':'L',
                        'basis':'实测' if r['volume_basis']=='actual' else '计划估算',
                        'state':{'fermenting':'发酵中','awaiting_packaging':'待包装','raw_ready':'酿造完成 · 在罐','in_tank':'生产中 · 在罐'}.get(r['production_status'],'在罐')})
        elif code in {'mash','fermentation'}: rows = self._production(code,factories)
        elif code=='purchase':
            records = self.env['purchase.order'].sudo().search(self._scope(factories,'brew_factory_id'))
            rows = [{'id':str(r.id),'at':r.date_order.isoformat() if r.date_order else r.create_date.isoformat(),'reference':r.name,
                     'factory':r.brew_factory_id.name,'supplier':r.partner_id.name,'amount':r.amount_total,
                     'currency':{'CNY':'人民币','USD':'美元','EUR':'欧元'}.get(r.currency_id.name,r.currency_id.symbol),
                     'state':STATES.get(r.state,'待核对')} for r in records]
        else: rows = self._inventory(code,factories)
        keyword = (filters.get('keyword') or '').strip().lower()
        def matches(row):
            if definition['dated']:
                at = parse_time(row['at']) if row.get('at') else None
                if (first or last) and (not at or (first and at<first) or (last and at>=last)): return False
            return not keyword or any(keyword in str(row.get(k) or '').lower() for k in ['product','batch','lot','location','reference','supplier','operation','operator','material'])
        rows = [r for r in rows if matches(r)]
        rows.sort(key=lambda r:(r.get('at') or '',r.get('id') or ''),reverse=True)
        totals = {}
        for row in rows:
            if row.get('unit') and row.get('quantity') is not None:
                totals[row['unit']] = totals.get(row['unit'],0)+row['quantity']
        total = len(rows)
        if export:
            if total>20000: raise ApiError('EXPORT_TOO_LARGE','结果超过两万条，请缩小日期或酒厂范围后导出。',422)
            return definition,rows
        return {'definition':definition,'rows':rows[(page-1)*size:page*size],'total':total,'page':page,'page_size':size,
                'unit_totals':[{'unit':u,'quantity':q} for u,q in totals.items()], 'as_of':fields.Datetime.now().isoformat()}

    def csv(self,code,filters):
        definition,rows = self.data(code,filters,export=True)
        stream = io.StringIO(); writer = csv.writer(stream)
        writer.writerow([c['label'] for c in definition['columns']])
        for row in rows:
            cells = []
            for c in definition['columns']:
                value = row.get(c['key'],'')
                if c['key'] in {'at','expires','completed_at'} and value:
                    value = parse_time(value).replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(self.user.tz or 'Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
                if isinstance(value,str) and value[:1] in '=+-@': value="'"+value
                cells.append(value)
            writer.writerow(cells)
        return definition['name'], ('\ufeff'+stream.getvalue()).encode('utf-8')
