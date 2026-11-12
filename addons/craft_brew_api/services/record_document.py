"""Explicit business fields for read-only workbench document details."""
META = [('partner_id','供应商'),('requester_id','申请人'),('purchase_order_id','采购订单'),('production_plan_id','生产计划'),('location_id','库位'),('source_location_id','来源库位'),('destination_location_id','目标库位'),('production_location_id','生产库位'),('date_order','下单时间'),('date_planned','预计到货'),('reason','原因'),('note','备注')]
QUANTITIES = [('product_qty','采购数量'),('requested_qty','需求数量'),('approved_qty','批准数量'),('ordered_qty','已下单'),('qty_received','已收货'),('received_qty','实收'),('qualified_qty','合格'),('rejected_qty','不合格'),('returned_qty','退货 / 退料'),('quantity','数量'),('recommended_qty','推荐数量'),('issued_qty','已发料'),('acknowledged_qty','实领'),('consumed_qty','耗用'),('loss_qty','损耗'),('theoretical_qty','账面数量'),('counted_qty','盘点数量'),('difference_qty','差异')]

def display(record, key):
    value = record[key]
    field = record._fields[key]
    if field.type == 'many2one': return value.display_name if value else ''
    if field.type == 'selection':
        if record._name == 'purchase.order' and key == 'state':
            return {'draft':'草稿','sent':'待确认','to approve':'待审批','purchase':'已正式下单','done':'已完成','cancel':'已取消'}.get(value,'处理中')
        label = dict(field._description_selection(record.env)).get(value, '')
        if any('\u4e00' <= c <= '\u9fff' for c in str(label)):return label
        return {'draft':'草稿','pending':'待处理','submitted':'待审批','approved':'审批通过','returned':'已退回','rejected':'未通过','in_progress':'执行中','done':'已完成','completed':'已完成','cancelled':'已取消','cancel':'已取消','issued':'已发料','acknowledged':'已确认实领','confirmed':'已确认','transferred':'已转交','released':'已放行','pending_release':'待放行','ordered':'已下单','closed':'已关闭'}.get(value,'处理中')
    if field.type in {'datetime','date'}: return value.isoformat() if value else ''
    return value if value is not False and value is not None else ''

def document_details(source, instance=None):
    if not source: return None
    metadata = [{'label': label, 'value': display(source,key)} for key,label in META if key in source._fields and source[key]]
    stats=[]
    currency=source.currency_id.symbol if 'currency_id' in source._fields and source.currency_id else ''
    for key,label in [('amount_total','订单总额'),('amount_untaxed','未税金额'),('amount_tax','税额')]:
        if key in source._fields:stats.append({'label':label,'value':source[key],'unit':currency})
    lines=source.order_line if source._name=='purchase.order' else source.line_ids if 'line_ids' in source._fields else []
    result=[]
    for line in lines:
        if 'display_type' in line._fields and line.display_type:continue
        product=line.product_id if 'product_id' in line._fields else None
        unit=next((line[k].name for k in ['uom_id','product_uom'] if k in line._fields and line[k]),'')
        unit={'Units':'件','Unit':'件','Units.':'件','Litre':'L','Liters':'L','kg':'kg','g':'g'}.get(unit,unit)
        values=[{'label':label,'value':line[key],'unit':unit} for key,label in QUANTITIES if key in line._fields]
        for key,label in [('price_unit','单价'),('price_subtotal','小计')]:
            if key in line._fields:values.append({'label':label,'value':line[key],'unit':currency})
        allocations=[]
        if 'allocation_ids' in line._fields:
            for a in line.allocation_ids:
                allocations.append({'lot':a.lot_id.name if 'lot_id' in a._fields else '', 'location':a.location_id.display_name if 'location_id' in a._fields else '', 'values':[{'label':label,'value':a[key],'unit':unit} for key,label in QUANTITIES if key in a._fields]})
        result.append({'name':product.display_name if product else display(line,'name') if 'name' in line._fields else '明细', 'lot':line.lot_id.name if 'lot_id' in line._fields and line.lot_id else '', 'values':values,'allocations':allocations})
    if instance is None and 'approval_instance_id' in source._fields:instance=source.approval_instance_id
    if instance is None and 'brew_approval_instance_id' in source._fields:instance=source.brew_approval_instance_id
    approvals=[]
    if instance:
        for step in instance.step_ids.sorted('sequence'):
            approvals.append({'role':ROLE_NAMES.get(step.role_code,'审批人'),'state':display(step,'state'),'by':step.acted_by_id.name or '', 'at':step.acted_at.isoformat() if step.acted_at else None,'comment':step.comment or ''})
    return {'name':source.display_name,'state':display(source,'state') if 'state' in source._fields else '', 'metadata':metadata,'stats':stats,'lines':result,'approvals':approvals}

ROLE_NAMES={'general_manager':'总经理','production_director':'生产总监','brewery_manager':'酒厂负责人','tenant_admin':'管理员','warehouse_keeper':'库管','purchaser':'采购员','finance':'财务'}
