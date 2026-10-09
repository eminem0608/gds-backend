import math
from odoo import fields
from .master_data import MasterDataService
from ..controllers.base import ApiError

TYPES = {
 'bottle':('容器','玻璃瓶',['capacity','material','closure']), 'can':('容器','易拉罐',['capacity','material','closure']), 'keg':('容器','桶',['capacity','material','closure']),
 'cap':('封口配件','瓶盖',['closure']), 'can_lid':('封口配件','罐盖',['closure']), 'keg_seal':('封口配件','桶盖 / 封签',['closure']),
 'label':('标签','标签',['position','dimensions','material']), 'carton':('外包装','纸箱 / 礼盒',['dimensions','pack_count']), 'divider':('外包装','隔板 / 纸托',['dimensions']),
 'film':('辅材','缠绕膜',['dimensions']), 'strap':('辅材','打包带',['dimensions'])
}
LABELS={'capacity':'容量','material':'材质','closure':'口径 / 接口','position':'标签位置','dimensions':'尺寸','pack_count':'装载数量'}
GROUPS=['craft_brew_core.group_brew_admin','craft_brew_core.group_brew_production_director','craft_brew_core.group_brew_purchase']

class PackagingService:
 def __init__(self,env,user,company):
  self.env,self.user,self.company=env,user,company
  self.supplier=user.has_group('craft_brew_core.group_brew_supplier')
  self.manager=any(user.has_group(g) for g in GROUPS)
  if not self.supplier and not self.manager and not user.has_group('craft_brew_core.group_brew_warehouse'):
   raise ApiError('PERMISSION_DENIED','无权查看包装材料。',403)
  if self.supplier and (not user.brew_supplier_id or user.brew_supplier_id.company_id!=company):
   raise ApiError('DATA_SCOPE_DENIED','账号未关联当前企业供应商。',403)
 def domain(self):
  domain=[('company_id','=',self.company.id),('brew_item_type','=','packaging_material')]
  if self.supplier: domain.append(('brew_supplier_id','=',self.user.brew_supplier_id.id))
  return domain
 def model(self): return self.env['product.template'].sudo()
 def record(self,id):
  record=self.model().with_context(active_test=False).search(self.domain()+[('id','=',int(id))],limit=1)
  if not record: raise ApiError('RESOURCE_NOT_FOUND','包装材料不存在。',404)
  return record
 def serialize(self,p):
  return {'id':str(p.id),'product_id':str(p.product_variant_id.id),'name':p.name,'code':p.default_code,'type':p.brew_packaging_type,'category_code':p.brew_material_category,'details':p.brew_packaging_details or {},'state':p.brew_packaging_state or 'confirmed','review_note':p.brew_packaging_review_note,'supplier':{'id':str(p.brew_supplier_id.id),'name':p.brew_supplier_id.name} if p.brew_supplier_id else None,'parent':{'id':str(p.brew_packaging_parent_id.id),'name':p.brew_packaging_parent_id.name} if p.brew_packaging_parent_id else None,'unit':p.uom_id.name,'active':p.active,'version':p.write_date.isoformat(),'image':p.image_128.decode() if p.image_128 else None}
 def list(self,page,size,keyword='',include_inactive=False):
  domain=self.domain()+([('name','ilike',keyword)] if keyword else [])
  model=self.model().with_context(active_test=not include_inactive)
  rows=model.search(domain,order='write_date desc,id desc',offset=(page-1)*size,limit=size)
  return {'items':[self.serialize(p) for p in rows],'total':model.search_count(domain),'can_manage':self.manager and not self.supplier}
 def schema(self):
  products=self.env['product.template'].sudo().search([('company_id','=',self.company.id),('brew_item_type','=','finished_product')])
  categories=self.env['brew.material.category'].sudo().search([('material_group','=','packaging'),('active','=',True),('parent_id','!=',False)])
  return {'categories':[{'code':c.code,'name':c.name,'type':c.brew_packaging_type} for c in categories], 'types':[{'code':k,'group':v[0],'name':v[1],'required':v[2]} for k,v in TYPES.items()], 'beers':[{'id':str(p.id),'name':p.name} for p in products], 'parents':[] if self.supplier else [{'id':str(p.id),'name':p.name,'type':p.brew_packaging_type} for p in self.model().search([('company_id','=',self.company.id),('brew_item_type','=','packaging_material'),('brew_packaging_state','=','confirmed'),('brew_supplier_id','=',False)])], 'suppliers':[] if self.supplier else [{'id':str(p.id),'name':p.name} for p in self.env['res.partner'].sudo().search([('company_id','=',self.company.id),('supplier_rank','>',0),('active','=',True)])]}
 def check_version(self,p,payload):
  self.env.cr.execute('SELECT id FROM product_template WHERE id = %s FOR UPDATE',[p.id]);p.invalidate_recordset()
  if payload.get('version')!=p.write_date.isoformat(): raise ApiError('VERSION_CONFLICT','记录已变化，请刷新后重试。',409)
 def values(self,payload):
  name=str(payload.get('name') or '').strip();kind=payload.get('type');d=payload.get('details') or {}
  category=self.env['brew.material.category'].sudo().search([('code','=',payload.get('category_code')),('material_group','=','packaging'),('parent_id','!=',False),('active','=',True)],limit=1)
  if not category or category.brew_packaging_type!=kind: raise ApiError('VALIDATION_REQUIRED','请选择原料种类中有效的包装小类。',422)
  if not name or kind not in TYPES or not isinstance(d,dict): raise ApiError('VALIDATION_REQUIRED','名称和包装类型不能为空。',422)
  allowed={'capacity','capacity_unit','material','closure','dimensions','color','position','pack_count','custom','beer_product_id','artwork_version','procurement_unit','unit_quantity','quote_price','min_order','lead_days','supplier_code','unit','notes','artwork_name','artwork_data'}
  if set(d)-allowed: raise ApiError('VALIDATION_FORMAT','包装规格字段无效。',422)
  for key in TYPES[kind][2]:
   if d.get(key) is None or str(d.get(key)).strip()=='': raise ApiError('VALIDATION_REQUIRED',LABELS[key]+'不能为空。',422)
  try:
   for key in ['capacity','pack_count','unit_quantity','quote_price','min_order','lead_days']:
    if d.get(key) not in [None,'']:
     if isinstance(d[key],bool): raise ValueError()
     value=float(d[key])
     if not math.isfinite(value) or value<0 or (key in ['capacity','pack_count','unit_quantity'] and value<=0): raise ValueError()
     if key in ['pack_count','lead_days'] and not value.is_integer(): raise ValueError()
     d[key]=value
  except (ValueError,TypeError): raise ApiError('VALIDATION_FORMAT','容量、数量及价格须为有效数值。',422)
  if kind in ['bottle','can','keg'] and d.get('capacity_unit') not in ['mL','L']: raise ApiError('VALIDATION_REQUIRED','请选择容量单位。',422)
  if not d.get('procurement_unit') or not d.get('unit_quantity') or d.get('quote_price') in [None,'']: raise ApiError('VALIDATION_REQUIRED','请填写采购单位、换算数量及报价。',422)
  d.setdefault('custom',False)
  if not isinstance(d['custom'],bool): raise ApiError('VALIDATION_FORMAT','定制标记无效。',422)
  if d.get('custom'):
   beer=self.env['product.template'].sudo().search([('id','=',int(d.get('beer_product_id') or 0)),('company_id','=',self.company.id),('brew_item_type','=','finished_product')],limit=1)
   if not beer or not d.get('artwork_version'): raise ApiError('VALIDATION_REQUIRED','定制包装须选择啤酒产品并填写图稿版本。',422)
   d['beer_product_id']=str(beer.id)
  unit=d.get('unit','个')
  if kind not in ['film','strap'] and unit not in ['个','张']: raise ApiError('VALIDATION_FORMAT','容器、配件、标签及外箱按个或张管理。',422)
  if kind=='label' and unit!='张' or kind not in ['film','strap','label'] and unit!='个': raise ApiError('VALIDATION_FORMAT','标签按张、容器和配件按个管理。',422)
  if unit in ['个','张'] and not float(d['unit_quantity']).is_integer(): raise ApiError('VALIDATION_FORMAT','按个或张管理的采购换算须为整数。',422)
  d['unit']=unit
  refs={'个':'uom.product_uom_unit','张':'uom.product_uom_unit','kg':'uom.product_uom_kgm','m':'uom.product_uom_meter'}
  if unit not in refs: raise ApiError('VALIDATION_FORMAT','库存单位无效。',422)
  for key in ['artwork_data']:
   if len(str(d.get(key) or ''))>4000000: raise ApiError('VALIDATION_FORMAT','图稿附件请控制在 3 MB 以内。',422)
  supplier=self.user.brew_supplier_id if self.supplier else self.env['res.partner'].sudo().search([('id','=',int(payload.get('supplier_id') or 0)),('company_id','=',self.company.id),('supplier_rank','>',0)],limit=1)
  if not self.supplier and payload.get('supplier_id') and not supplier: raise ApiError('DATA_SCOPE_DENIED','供应商不属于当前企业。',403)
  summary=' · '.join(str(d[k])+(' '+d.get('capacity_unit','') if k=='capacity' else '') for k in TYPES[kind][2])
  uom=self.env.ref(refs[unit])
  if payload.get('code'):
   duplicates=self.model().search([('company_id','=',self.company.id),('default_code','=',str(payload['code']).strip())])
   if any(str(p.id)!=str(payload.get('id') or '') for p in duplicates): raise ApiError('VALIDATION_DUPLICATE','材料编码已存在。',422)
  values={'name':name,'default_code':str(payload.get('code') or '').strip(),'company_id':self.company.id,'detailed_type':'product','tracking':'lot','brew_item_type':'packaging_material','brew_material_category':category.code,'brew_packaging_type':kind,'brew_packaging_details':d,'brew_specification':summary,'brew_supplier_id':supplier.id if supplier else False,'brew_supplier_price':d['quote_price']/d['unit_quantity'],'uom_id':uom.id,'uom_po_id':uom.id}
  if 'image' in payload:
   if len(str(payload['image'] or ''))>4000000: raise ApiError('VALIDATION_FORMAT','图片请控制在 3 MB 以内。',422)
   values['image_1920']=payload['image'] or False
  return values
 def save(self,payload,id=None):
  if not self.supplier and not self.manager: raise ApiError('PERMISSION_DENIED','无权维护包装材料。',403)
  p=self.record(id) if id else None
  if p:self.check_version(p,payload)
  values=self.values({**payload,'id':id})
  if p and (p.uom_id.id!=values['uom_id'] or (p.brew_packaging_details or {}).get('unit')!=values['brew_packaging_details']['unit']):
   if self.env['stock.move'].sudo().search_count([('product_id','=',p.product_variant_id.id)]): raise ApiError('STATE_PRECONDITION_FAILED','已有库存业务的材料不能修改库存单位，请新增规格。',422)
  values['brew_packaging_state']='draft' if self.supplier else 'confirmed'
  values['brew_packaging_review_note']=False
  if p:
   if self.supplier: values['brew_packaging_parent_id']=False
   p.write(values)
  else:p=self.model().with_context(no_create_variants=False).create(values)
  return p,self.serialize(p)
 def action(self,id,action,payload):
  p=self.record(id);self.check_version(p,payload)
  if action=='submit':
   if not self.supplier or p.brew_packaging_state not in ['draft','returned']: raise ApiError('STATE_PRECONDITION_FAILED','只有草稿或需修改商品可以提交。',422)
   self.values({'name':p.name,'type':p.brew_packaging_type,'category_code':p.brew_material_category,'details':p.brew_packaging_details})
   p.write({'brew_packaging_state':'submitted'})
  elif action in ['confirm','return']:
   if self.supplier or not self.manager: raise ApiError('PERMISSION_DENIED','仅酒厂管理人员可以确认供应商商品。',403)
   if p.brew_packaging_state!='submitted': raise ApiError('STATE_PRECONDITION_FAILED','商品不在待确认状态。',422)
   note=str(payload.get('note') or '').strip()
   if action=='return' and not note: raise ApiError('VALIDATION_REQUIRED','请填写退回原因。',422)
   parent_id=payload.get('parent_id')
   parent=self.model().search([('id','=',int(parent_id or 0)),('company_id','=',self.company.id),('brew_item_type','=','packaging_material'),('brew_packaging_state','=','confirmed'),('brew_supplier_id','=',False)],limit=1) if parent_id else None
   if parent_id and (not parent or parent==p or parent.brew_packaging_type!=p.brew_packaging_type): raise ApiError('VALIDATION_RELATION','关联材料类型或企业不匹配。',422)
   if parent:
    keys=TYPES[p.brew_packaging_type][2]+['capacity_unit','custom','beer_product_id','artwork_version','unit']
    if any((parent.brew_packaging_details or {}).get(k)!=(p.brew_packaging_details or {}).get(k) for k in keys): raise ApiError('VALIDATION_RELATION','关键规格不一致，不能关联同一材料。',422)
   p.write({'brew_packaging_state':'confirmed' if action=='confirm' else 'returned','brew_packaging_review_note':note,'brew_packaging_parent_id':parent.id if parent else False})
  elif action in ['archive','restore']:
   if not self.supplier and not self.manager: raise ApiError('PERMISSION_DENIED','无权修改状态。',403)
   p.write({'active':action=='restore'})
  else: raise ApiError('RESOURCE_NOT_FOUND','操作不存在。',404)
  return p,self.serialize(p)
