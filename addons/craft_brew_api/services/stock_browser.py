import base64
import json
from io import BytesIO
from PIL import Image
from odoo import fields
from .inventory import InventoryService
from .production_stock import liquid_balance
from ..controllers.base import ApiError

class StockBrowserService(InventoryService):
 def entry_options(self,factory_id=None):
  self._require_manage()
  factories=self.env['brew.factory'].sudo().browse(self._allowed_factory_ids())
  local_manager=self.user.has_group('craft_brew_core.group_brew_brewery_manager') and not (self.user.has_group('craft_brew_core.group_brew_admin') or self.user.has_group('craft_brew_core.group_brew_general_manager'))
  if local_manager:
   preferred=self.user.brew_default_factory_id.filtered(lambda f:f in factories)[:1]
   context_factory=self.env.context.get('brew_factory_id')
   if not preferred and context_factory: preferred=factories.filtered(lambda f:f.id==int(context_factory))[:1]
   preferred=preferred or factories[:1]
   if factory_id and preferred and str(preferred.id)!=str(factory_id): raise ApiError('DATA_SCOPE_DENIED','酒厂负责人只能为默认所属酒厂录入库存。',403)
   factories=preferred
  factory=self._factory(factory_id) if factory_id else (factories[:1] if len(factories)==1 else None)
  locations=self.env['stock.location'].sudo().search(self._location_domain(factory.id if factory else None)+[('brew_location_type','in',['raw_material','packaging','qc','semi_finished','finished'])],order='name,id')
  tank_location_ids=set(self.env['brew.tank'].sudo().search([('stock_location_id','in',locations.ids)]).mapped('stock_location_id').ids)
  locations=locations.filtered(lambda location: location.id not in tank_location_ids)
  products=self.env['product.product'].sudo().with_context(active_test=True).search([
   ('company_id','in',[False,self.company.id]),
   ('product_tmpl_id.brew_item_type','in',['raw_material','packaging_material','semi_finished','finished_product']),
  ],order='name,id')
  products=products.filtered(lambda p:not (p.brew_item_type=='packaging_material' and p.brew_packaging_catalog_owner_id))
  return {
   'factories':[{'id':str(f.id),'name':f.name} for f in factories],
   'locations':[{'id':str(l.id),'name':l.name,'type':l.brew_location_type,'factory_id':str(l.brew_factory_id.id),'factory_name':l.brew_factory_id.name} for l in locations if l.brew_factory_id],
   'products':[{'id':str(p.id),'name':p.display_name,'unit':(p.product_tmpl_id.brew_packaging_details or {}).get('unit') or p.uom_id.name,'tracking':p.tracking,'item_type':p.brew_item_type,'specification':p.product_tmpl_id.brew_specification or '','category':self.category(p.product_tmpl_id),'supplier':{'id':str(p.product_tmpl_id.brew_supplier_id.id),'name':p.product_tmpl_id.brew_supplier_id.name} if p.product_tmpl_id.brew_supplier_id else None,'brand':p.product_tmpl_id.brew_brand or '','price':p.product_tmpl_id.brew_supplier_price if p.product_tmpl_id.brew_supplier_id else None,'image_url':f'/api/v1/inventory/products/{p.id}/image' if p.image_1920 else None,'packages':[{'id':str(pack.id),'name':pack.name,'level':pack.brew_barcode_level or 'other','qty':pack.qty,'volume_l':pack.brew_volume_l} for pack in self.env['product.packaging'].sudo().search([('product_id','=',p.id),('active','=',True),('qty','>',0)],order='sequence,id')]} for p in products],
  }

 def product_image(self,product_id):
  product=self._product(product_id)
  if not product.image_256: raise ApiError('RESOURCE_NOT_FOUND','商品图片不存在。',404)
  content=base64.b64decode(product.image_256)
  with Image.open(BytesIO(content)) as image: mimetype=image.get_format_mimetype() or 'image/jpeg'
  return content,mimetype

 def _product(self,product_id):
  product=self.env['product.product'].sudo().with_context(active_test=False).search([('id','=',int(product_id)), '|', ('company_id','=',False),('company_id','=',self.company.id)],limit=1)
  if not product: raise ApiError('RESOURCE_NOT_FOUND','物料不存在。',404)
  if product.brew_item_type=='packaging_material' and product.brew_packaging_catalog_owner_id:
   raise ApiError('RESOURCE_NOT_FOUND','该商品不属于酒厂库存材料。',404)
  return product
 def category(self,p):
  if p.brew_item_type=='packaging_material':return 'packaging'
  if p.brew_item_type=='finished_product':return 'finished'
  if p.brew_item_type=='semi_finished':return 'semi_finished'
  c=self.env['brew.material.category'].sudo().search([('code','=',p.brew_material_category)],limit=1)
  return c.material_group if c else p.brew_material_category or 'other'
 def quants(self,factory=None,product=None):
  locations=self.env['stock.location'].sudo().search(self._location_domain(factory))
  domain=[('company_id','=',self.company.id),('location_id','in',locations.ids),('quantity','!=',0)]
  if product:domain.append(('product_id','=',int(product)))
  return self.env['stock.quant'].sudo().search(domain)
 def product_info(self,p):
  t=p.product_tmpl_id
  return {'id':str(p.id),'name':p.with_context(display_default_code=False).display_name,'code':p.default_code,'image_url':f'/api/v1/inventory/products/{p.id}/image' if p.image_1920 else None,'category':self.category(t),'specification':t.brew_specification,'unit':(t.brew_packaging_details or {}).get('unit') or p.uom_id.name,'supplier':{'id':str(t.brew_supplier_id.id),'name':t.brew_supplier_id.name} if t.brew_supplier_id else None,'beer_type':' / '.join(t.brew_beer_category_id.mapped('name')+t.brew_beer_subcategory_id.mapped('name')),'important_details':self.important_details(t),'packaging_type':t.brew_packaging_type,'packaging_details':{k:v for k,v in (t.brew_packaging_details or {}).items() if k not in ['artwork_data']}}
 def important_details(self,t):
  category=self.category(t)
  names={'malt':'麦芽','adjunct':'可发酵物 / 辅料','hop':'啤酒花','yeast':'酵母','packaging':'包装材料','finished':'成品酒','semi_finished':'原液库存','other':'其他材料'}
  kinds={'bottle':'酒瓶','can':'易拉罐','keg':'酒桶','carton':'纸箱 / 礼盒'}
  result=[{'label':'材料类型','value':kinds.get(t.brew_packaging_type,names.get(category,'其他材料'))}]
  def add(label,value):
   if value is not None and value is not False and value!='':result.append({'label':label,'value':str(value)})
  add('供应商',t.brew_supplier_id.name or '见入库批次')
  unit=(t.brew_packaging_details or {}).get('unit') or t.uom_id.name
  add('库存单位',unit)
  if category=='packaging':
   d=t.brew_packaging_details or {}
   if d.get('quote_price') is not None and d.get('quote_price')!='':
    price=float(d['quote_price'])/float(d.get('unit_quantity') or 1)
    add('单价',f'¥{price:g} / {unit}')
   if t.brew_packaging_type in {'bottle','can','keg'}:
    if d.get('capacity'):add('容量',f"{float(d['capacity']):g} {d.get('capacity_unit') or ''}")
    add('材质',d.get('material'))
    add('颜色',d.get('color'))
    dimensions=[]
    if d.get('height_mm'):dimensions.append(f"高 {float(d['height_mm']):g} mm")
    if d.get('diameter_mm'):dimensions.append(f"直径 {float(d['diameter_mm']):g} mm")
    if dimensions:add('尺寸',' · '.join(dimensions))
   elif t.brew_packaging_type=='carton':
    add('尺寸',d.get('dimensions') or t.brew_specification)
    if d.get('dimensions'):add('尺寸口径',d.get('dimension_basis') or '内尺寸')
    if d.get('pack_count'):add('每箱数量',f"{float(d['pack_count']):g} 件")
    add('适用容器',d.get('fit_container_type'))
    if d.get('fit_capacity_ml'):add('适用容量',f"{float(d['fit_capacity_ml']):g} mL")
  else:
   add('品牌',t.brew_brand)
   add('规格',t.brew_specification)
   if t.brew_supplier_id and t.brew_supplier_price:add('单价',f'¥{t.brew_supplier_price:g} / {unit}')
   if category=='malt':
    if t.brew_color_ebc:add('色度',f'{t.brew_color_ebc:g} EBC')
    if t.brew_extract_rate:add('浸出率',f'{t.brew_extract_rate:g}%')
   elif category=='hop' and t.brew_alpha_acid:add('α酸含量',f'{t.brew_alpha_acid:g}%')
   elif category=='yeast':add('风味',t.brew_flavor_desc)
   elif category in {'finished','semi_finished'}:
    add('酒款分类',' / '.join(t.brew_beer_category_id.mapped('name')+t.brew_beer_subcategory_id.mapped('name')))
    if t.brew_shelf_life_days:add('保质期',f'{t.brew_shelf_life_days} 天')
  return result
 def balances(self,quants,reservations):
  result={'on_hand':0,'reserved':0,'available':0,'blocked':0};today=fields.Date.today()
  buckets={}
  for q in quants:
   key=(q.location_id.id,q.product_id.id,q.lot_id.id)
   b=buckets.setdefault(key,{'quantity':0,'reserved':reservations.get(key,0),'lot':q.lot_id,'location_type':q.location_id.brew_location_type})
   b['quantity']+=q.quantity;b['reserved']+=q.reserved_quantity
  for b in buckets.values():
   lot=b['lot'];qty=b['quantity'];reserved=b['reserved']
   qualified=bool(lot and lot.brew_quality_status=='qualified' and not lot.brew_frozen and (not lot.brew_expiration_date or lot.brew_expiration_date>=today))
   if not lot and quants and quants[0].product_id.tracking=='none':qualified=True
   if b['location_type'] in {'qc','material_issue'}:qualified=False
   result['on_hand']+=qty;result['reserved']+=reserved
   result['available']+=max(qty-reserved,0) if qualified else 0
   result['blocked']+=max(qty,0) if not qualified else 0
  return result
 def production_liquids(self,filters):
  if filters.get('category') not in {None, '', 'semi_finished'}:return []
  allowed=self._allowed_factory_ids()
  if filters.get('factory_id'):allowed=[self._factory(filters['factory_id']).id]
  batch_domain=[('company_id','=',self.company.id),('factory_id','in',allowed),('state','in',['in_progress','in_tank','completed'])]
  if filters.get('_plan_id'):batch_domain.append(('production_plan_id','=',int(filters['_plan_id'])))
  if '_plan_ids' in filters:batch_domain.append(('production_plan_id','in',filters['_plan_ids']))
  batches=self.env['brew.production.batch'].sudo().search(batch_domain)
  tasks=self.env['brew.task'].sudo().search([('company_id','=',self.company.id),('target_model','=','brew.production.plan'),('target_id','in',batches.mapped('production_plan_id').ids),('task_type','in',['mashing','fermentation']),('state','!=','cancelled')])
  orders=self.env['brew.packaging.order'].sudo().search([('company_id','=',self.company.id),('batch_id','in',batches.ids),('state','in',['pending_release','released'])])
  rows=[];keyword=str(filters.get('keyword') or '').lower()
  for batch in batches:
   plan=batch.production_plan_id
   batch_tasks=tasks.filtered(lambda t:t.target_id==plan.id)
   ferment=batch_tasks.filtered(lambda t:t.task_type=='fermentation')[:1]
   ferment_data=json.loads(ferment.execution_checkpoints_json or '{}') if ferment else {}
   if not isinstance(ferment_data,dict):ferment_data={}
   mash=batch_tasks.filtered(lambda t:t.task_type=='mashing')[:1]
   nodes=json.loads(mash.execution_checkpoints_json or '[]') if mash else []
   if not isinstance(nodes,list):nodes=[]
   entered=bool(batch.entered_tank_at or batch.state in {'in_tank','completed'} or ferment_data.get('started_at') or any(n.get('code')=='transfer_fermenter' and n.get('state')=='done' for n in nodes))
   if not entered:continue
   completion=json.loads(plan.brewing_completion_json or '{}')
   actual=completion.get('actual_volume_l')
   if actual is None and batch.actual_volume_l>0:actual=batch.actual_volume_l
   if actual is None:
    actual=next((float(n['value']) for n in nodes if n.get('code')=='actual_volume' and n.get('state')=='done' and n.get('value') not in (None,'')),None)
   packs=orders.filtered(lambda o:o.batch_id==batch)
   packaged=sum(packs.mapped('output_volume_l'));loss=sum(packs.mapped('loss_volume_l'))
   packaging=json.loads(plan.packaging_plan_json or '{}')
   execution=packaging.get('execution') or {}
   # Historical task-only output is a fallback; linked orders are the source of truth.
   if execution.get('completed_at') and not execution.get('packaging_order_id'):
    packaged+=float(execution.get('output_volume_l') or 0);loss+=float(execution.get('loss_volume_l') or 0)
   balance=liquid_balance(actual,batch.planned_volume_l,packaged,loss,completion.get('disposition')=='emptied')
   if balance['volume_l']<=1e-8:continue
   info=self.product_info(plan.product_tmpl_id.product_variant_id)
   info={**info,'category':'semi_finished','unit':'L'}
   if keyword and keyword not in ' '.join([info['name'],batch.finished_product_code or '',batch.product_code_snapshot or '',batch.name,plan.name,plan.tank_id.name or '',plan.factory_id.name]).lower():continue
   has_packaging=bool(packaging.get('materials_only') or packaging.get('material_id') or packaging.get('packaging_id') or orders.filtered(lambda o:o.batch_id==batch))
   status='fermenting' if ferment and ferment.state=='in_progress' else ('awaiting_packaging' if has_packaging else 'raw_ready') if batch.state=='completed' or plan.state=='done' else 'in_tank'
   actual_tank=batch.fermentation_session_id.tank_id or plan.tank_id
   rows.append({'source_type':'production','batch_id':str(batch.id),'batch_name':batch.name,'production_date':(batch.started_at or plan.planned_start or batch.create_date).isoformat(),'finished_product_code':batch.finished_product_code,'plan_id':str(plan.id),'plan_name':plan.name,'tank':actual_tank.name or '', 'tank_id':str(actual_tank.id),'tank_verified':bool(actual_tank.current_production_batch_id==batch and actual_tank.current_volume_l>0), 'production_status':status,'product':info,'factory':{'id':str(plan.factory_id.id),'name':plan.factory_id.name},**balance,'on_hand':balance['volume_l'],'reserved':0,'available':0,'blocked':balance['volume_l'],'lot_count':1,'location_count':1,'earliest_expiration':None})
  return rows
 def list(self,filters,page,size):
  quants=self.quants(filters.get('factory_id'));res=self._reservation_totals(quants.mapped('location_id').ids) if quants else {}
  orders=self.env['brew.packaging.order'].sudo().search([('company_id','=',self.company.id),('factory_id','in',self._allowed_factory_ids()),('finished_lot_id','in',quants.mapped('lot_id').ids),('state','!=','cancelled')])
  grouped={}
  for q in quants:
   grouped.setdefault((q.location_id.brew_factory_id.id,q.product_id.id),self.env['stock.quant'].sudo().browse())
   grouped[(q.location_id.brew_factory_id.id,q.product_id.id)]|=q
  rows=[];keyword=str(filters.get('keyword') or '').lower()
  for (fid,pid),qs in grouped.items():
   p=qs[0].product_id;info=self.product_info(p)
   if p.brew_item_type == 'packaging_material' and p.product_tmpl_id.brew_packaging_type not in {'bottle','can','keg','carton'}:continue
   if filters.get('category') and info['category']!=filters['category']:continue
   linked=orders.filtered(lambda o:o.factory_id.id==fid and o.finished_lot_id in qs.mapped('lot_id'))
   matching=linked.filtered(lambda o:keyword and keyword in (o.batch_id.finished_product_code or '').lower())
   if matching:
    qs=qs.filtered(lambda q:q.lot_id in matching.mapped('finished_lot_id'))
    linked=matching
   batch_codes=sorted({code for code in linked.mapped('batch_id.finished_product_code') if code})
   if keyword and keyword not in ' '.join([info['name'],info['code'] or '',info['specification'] or '',qs[0].location_id.brew_factory_id.name]+qs.mapped('lot_id.name')+batch_codes).lower():continue
   dates=qs.mapped('lot_id.brew_expiration_date');dates=[d for d in dates if d]
   balance=self.balances(qs,res)
   rows.append({'product':info,'finished_product_codes':batch_codes,'factory':{'id':str(fid),'name':qs[0].location_id.brew_factory_id.name},**balance,'lot_count':len(qs.mapped('lot_id')),'location_count':len(qs.mapped('location_id')),'earliest_expiration':min(dates).isoformat() if dates else None})
  rows.extend(self.production_liquids(filters))
  rows.sort(key=lambda r:(r['factory']['name'],r['product']['name'],r.get('batch_name','')))
  factories=self.env['brew.factory'].sudo().browse(self._allowed_factory_ids())
  units={}
  for row in rows:
   u=units.setdefault(row['product']['unit'],{'unit':row['product']['unit'],'on_hand':0,'available':0,'reserved':0,'in_production':0,'estimated':0})
   for k in ['on_hand','available','reserved']:u[k]+=row[k]
   if row.get('source_type')=='production':
    u['in_production']+=row['on_hand']
    if row['volume_basis']=='planned':u['estimated']+=row['on_hand']
  return {'items':rows[(page-1)*size:page*size],'total':len(rows),'units':list(units.values()),'factories':[{'id':str(f.id),'name':f.name} for f in factories]}
 def detail(self,product_id,factory_id,history_page=1):
  factory=self._factory(factory_id);p=self._product(product_id);qs=self.quants(factory.id,p.id)
  res=self._reservation_totals(qs.mapped('location_id').ids,p.id) if qs else {}
  locations=self.env['stock.location'].sudo().search(self._location_domain(factory.id))
  lots=[]
  for lot in qs.mapped('lot_id'):
   lqs=qs.filtered(lambda q:q.lot_id==lot);data=self._serialize_lot(lot,lqs);data['uom']=self.product_info(p)['unit']
   packaging=self.env['brew.packaging.order'].sudo().search([('finished_lot_id','=',lot.id),('company_id','=',self.company.id),('factory_id','=',factory.id)],limit=1)
   data['source_batch']=packaging.batch_id.name if packaging else None
   data['finished_product_code']=packaging.batch_id.finished_product_code if packaging else None
   data['packaging']=packaging.packaging_id.name if packaging else None
   data['produced_at']=packaging.released_at.isoformat() if packaging and 'released_at' in packaging._fields and packaging.released_at else None
   data['supplier']=p.product_tmpl_id.brew_supplier_id.name or None
   receipt=self.env['brew.receipt.line'].sudo().search([('lot_id','=',lot.id),('receipt_id.company_id','=',self.company.id),('receipt_id.factory_id','=',factory.id)],limit=1,order='id desc')
   if receipt:data['supplier']=receipt.receipt_id.purchase_order_id.partner_id.name
   lots.append(data)
  moves=self.env['stock.move.line'].sudo().search([('product_id','=',p.id),('state','=','done'),'|',('location_id','in',locations.ids),('location_dest_id','in',locations.ids)],limit=50,order='date desc,id desc')
  reservations=self.env['brew.inventory.reservation'].sudo().search([('company_id','=',self.company.id),('factory_id','=',factory.id),('product_id','=',p.id),('state','=','active')])
  history=[];history_total=0
  if p.brew_item_type in ['finished_product','semi_finished']:
   domain=[('company_id','=',self.company.id),('factory_id','=',factory.id),'|',('product_tmpl_id','=',p.product_tmpl_id.id),('production_plan_id.product_tmpl_id','=',p.product_tmpl_id.id)]
   model=self.env['brew.production.batch'].sudo();history_total=model.search_count(domain)
   for batch in model.search(domain,order='create_date desc,id desc',limit=20,offset=(history_page-1)*20):
    orders=self.env['brew.packaging.order'].sudo().search([('batch_id','=',batch.id),('company_id','=',self.company.id)])
    history.append({'id':str(batch.id),'name':batch.name,'state':batch.state,'created_at':batch.create_date.isoformat(),'planned_volume_l':batch.planned_volume_l,'actual_volume_l':batch.actual_volume_l,'tank':batch.tank_id.name,'recipe':batch.recipe_version_id.name,'plan':batch.production_plan_id.name,'packaging':[{'name':o.name,'specification':o.packaging_id.name,'lot':o.finished_lot_id.name,'quantity':o.output_qty,'volume_l':o.output_volume_l,'state':o.state} for o in orders]})
  return {'product':self.product_info(p),'factory':{'id':str(factory.id),'name':factory.name},**self.balances(qs,res),'lots':lots,'locations':[{'id':str(l.id),'name':l.complete_name,**self.balances(qs.filtered(lambda q:q.location_id==l),res)} for l in qs.mapped('location_id')],'reservations':[{'id':str(r.id),'quantity':r.quantity_base,'source':r.source_model,'source_id':str(r.source_id),'expires_at':r.expires_at.isoformat() if r.expires_at else None} for r in reservations],'movements':[self._serialize_move_line(m) for m in moves],'production_history':history,'history_total':history_total,'history_page':history_page}
