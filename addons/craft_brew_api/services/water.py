import json
from odoo import fields
from odoo.addons.craft_brew_workflow.models.water_chemistry import validate_source, validate_adjustment, stage_snapshot, SALTS, IONS, VERSION
from ..controllers.base import ApiError

class WaterService:
 def __init__(self,env,user,company):self.env,self.user,self.company=env,user,company
 def headquarters(self):
  return self.user.brew_has_headquarters_scope(self.company)
 def factories(self):
  return self.user.brew_allowed_factories(self.company)
 def can_manage(self):return any(self.user.has_group('craft_brew_core.group_brew_'+code) for code in ['admin','general_manager','production_director','brewery_manager','brewer'])
 def factory(self,identifier):
  try:identifier=int(identifier)
  except (TypeError,ValueError):raise ApiError('VALIDATION_FORMAT','请选择酒厂。',422)
  factory=self.factories().filtered(lambda f:f.id==identifier)
  if not factory:raise ApiError('DATA_SCOPE_DENIED','无权访问该酒厂的水源。',403)
  return factory
 def serialize(self,source):
  return {'id':str(source.id),'name':source.name,'factory_id':str(source.factory_id.id),'factory_name':source.factory_id.name,'active':source.active,'is_default':source.is_default,'source_type':source.source_type,'measured_on':source.measured_on.isoformat(),'report_reference':source.report_reference or '', 'notes':source.notes or '',**json.loads(source.quality_json),'version':source.write_date.isoformat() if source.write_date else None}
 def list(self,factory_id=None):
  factories=self.factories();factory=self.factory(factory_id) if factory_id else None
  sources=self.env['brew.water.source'].sudo().with_context(active_test=False).search([('company_id','=',self.company.id),('factory_id','in',factory.ids if factory else factories.ids)],order='is_default desc,name,id')
  return {'items':[self.serialize(s) for s in sources],'factories':[{'id':str(f.id),'name':f.name} for f in factories],'can_manage':self.can_manage(),'salts':SALTS,'ions':IONS,'calculation_version':VERSION}
 def save(self,payload,identifier=None):
  if not self.can_manage():raise ApiError('PERMISSION_DENIED','只有总部管理人员或本厂负责人可以修改水源档案。',403)
  factory=self.factory(payload.get('factory_id'))
  self.env.cr.execute('SELECT id FROM brew_factory WHERE id=%s FOR UPDATE',[factory.id])
  source=None
  if identifier:
   source=self.env['brew.water.source'].sudo().with_context(active_test=False).search([('id','=',identifier),('company_id','=',self.company.id)],limit=1)
   if not source:raise ApiError('RESOURCE_NOT_FOUND','水源不存在。',404)
   self.factory(source.factory_id.id)
   if source.factory_id!=factory:raise ApiError('VALIDATION_RELATION','已有水源不能变更所属酒厂。',422)
   source.invalidate_recordset()
   if payload.get('version')!=(source.write_date.isoformat() if source.write_date else None):raise ApiError('VERSION_CONFLICT','水源已被更新，请刷新后再修改。',409)
  name=str(payload.get('name') or '').strip()
  if not name:raise ApiError('VALIDATION_REQUIRED','请填写水源名称。',422)
  quality={key:payload.get(key) for key in ['ions','ph','alkalinity']}
  try:
   validate_source(quality)
   measured=fields.Date.to_date(payload.get('measured_on'))
   if not measured or measured>fields.Date.today():raise ValueError('检测日期不能为空或晚于今天。')
  except (TypeError,ValueError) as exc:raise ApiError('VALIDATION_FORMAT',str(exc),422)
  active=payload.get('active',True) is True
  default=payload.get('is_default',False) is True and active
  if default:self.env['brew.water.source'].sudo().search([('factory_id','=',factory.id),('company_id','=',self.company.id),('is_default','=',True)]).write({'is_default':False})
  values={'name':name,'company_id':self.company.id,'factory_id':factory.id,'source_type':payload.get('source_type','treated'),'measured_on':measured,'report_reference':str(payload.get('report_reference') or ''),'notes':str(payload.get('notes') or ''),'quality_json':json.dumps(quality,ensure_ascii=False),'is_default':default,'active':active}
  if values['source_type'] not in {'tap','well','ro','treated','other'}:raise ApiError('VALIDATION_FORMAT','水源类型无效。',422)
  if source:
   history=json.loads(source.history_json or '[]')
   history.append(self.serialize(source))
   values['history_json']=json.dumps(history,ensure_ascii=False)
   source.write(values)
  else:source=self.env['brew.water.source'].sudo().create(values)
  return self.serialize(source)
 def validate_recipe(self,water,equipment):
  if not water or not water.get('enabled'):return
  source=water.get('source') or {}
  if water.get('source_mode') not in {'factory','override'}:raise ApiError('VALIDATION_FORMAT','原水来源方式无效。',422)
  self.factory(equipment.brew_factory_id.id)
  if str(source.get('factory_id'))!=str(equipment.brew_factory_id.id):raise ApiError('VALIDATION_RELATION','水源所属酒厂须与酿造设备一致，请重新选择水源。',422)
  if source.get('id'):
   try:identifier=int(source['id'])
   except (ValueError,TypeError):raise ApiError('VALIDATION_FORMAT','水源标识无效。',422)
   record=self.env['brew.water.source'].sudo().with_context(active_test=False).search([('id','=',identifier),('company_id','=',self.company.id),('factory_id','=',equipment.brew_factory_id.id)],limit=1)
   if not record:raise ApiError('VALIDATION_RELATION','水源不存在或不属于当前酒厂。',422)
   if water.get('source_mode')=='factory':
    candidates=[json.loads(record.quality_json)]+json.loads(record.history_json or '[]')
    def same(profile):
     try:
      return all(float(source['ions'][k])==float(profile['ions'][k]) for k in IONS) and all((None if source.get(k) in (None,'') else float(source[k]))==(None if profile.get(k) in (None,'') else float(profile[k])) for k in ['ph','alkalinity'])
     except (ValueError,TypeError,KeyError):return False
    if not any(same(profile) for profile in candidates):raise ApiError('VALIDATION_RELATION','原水已手动修改，请填写修改依据并以本配方调整方式保存。',422)

  elif water.get('source_mode')!='override':raise ApiError('VALIDATION_REQUIRED','请选择酒厂水源，或填写手动原水数据及依据。',422)

 def target_product(self,identifier):
  try:identifier=int(identifier)
  except (TypeError,ValueError):raise ApiError('VALIDATION_FORMAT','请选择啤酒产品。',422)
  product=self.env['product.template'].sudo().with_context(active_test=False).search([('id','=',identifier),('company_id','in',[False,self.company.id]),('brew_item_type','in',['finished_product','semi_finished'])],limit=1)
  if not product:raise ApiError('RESOURCE_NOT_FOUND','啤酒产品不存在或不属于当前企业。',404)
  return product
 def serialize_target(self,target):
  return {'id':str(target.id),'name':target.name,'product_id':str(target.product_tmpl_id.id),'product_name':target.product_tmpl_id.name,'factory_id':str(target.factory_id.id) if target.factory_id else '', 'factory_name':target.factory_id.name if target.factory_id else '总部通用','active':target.active,'is_default':target.is_default,'ions':json.loads(target.ranges_json),'notes':target.notes or '', 'version':target.write_date.isoformat() if target.write_date else None,'can_edit':self.can_manage() and (self.headquarters() or bool(target.factory_id and target.factory_id in self.factories()))}
 def targets(self,product_id=None,factory_id=None):
  factories=self.factories();factory=self.factory(factory_id) if factory_id else None
  domain=[('company_id','=',self.company.id),'|',('factory_id','=',False),('factory_id','in',factory.ids if factory else factories.ids)]
  if product_id:domain.append(('product_tmpl_id','=',self.target_product(product_id).id))
  targets=self.env['brew.water.target'].sudo().with_context(active_test=False).search(domain,order='is_default desc,name,id')
  products=self.env['product.template'].sudo().search([('company_id','in',[False,self.company.id]),('brew_item_type','in',['finished_product','semi_finished']),('active','=',True)],order='name,id')
  return {'items':[self.serialize_target(t) for t in targets],'products':[{'id':str(p.id),'name':p.name} for p in products],'factories':[{'id':str(f.id),'name':f.name} for f in factories],'can_manage':self.can_manage(),'can_manage_common':self.headquarters()}
 def save_target(self,payload,identifier=None):
  if not self.can_manage():raise ApiError('PERMISSION_DENIED','当前账号不能修改水质目标方案。',403)
  product=self.target_product(payload.get('product_id'))
  factory=self.factory(payload['factory_id']) if payload.get('factory_id') else None
  if not factory and not self.headquarters():raise ApiError('PERMISSION_DENIED','总部通用方案仅总部管理人员可维护；请选择所属酒厂。',403)
  self.env.cr.execute('SELECT id FROM product_template WHERE id=%s FOR UPDATE',[product.id])
  target=None
  if identifier:
   target=self.env['brew.water.target'].sudo().with_context(active_test=False).search([('id','=',identifier),('company_id','=',self.company.id)],limit=1)
   if not target:raise ApiError('RESOURCE_NOT_FOUND','水质目标方案不存在。',404)
   if target.factory_id:self.factory(target.factory_id.id)
   elif not self.headquarters():raise ApiError('PERMISSION_DENIED','无权修改总部通用目标方案。',403)
   if target.product_tmpl_id!=product or target.factory_id!=(factory or self.env['brew.factory']):raise ApiError('VALIDATION_RELATION','已有目标方案不能更换酒品或适用范围，请新增方案。',422)
   target.invalidate_recordset()
   if payload.get('version')!=(target.write_date.isoformat() if target.write_date else None):raise ApiError('VERSION_CONFLICT','目标方案已被更新，请刷新后再修改。',409)
  name=str(payload.get('name') or '').strip()
  if not name:raise ApiError('VALIDATION_REQUIRED','请填写目标方案名称。',422)
  from odoo.addons.craft_brew_workflow.models.water_chemistry import numeric
  ions=payload.get('ions');ranges={};count=0
  if not isinstance(ions,dict) or set(ions)-set(IONS):raise ApiError('VALIDATION_FORMAT','目标离子格式无效。',422)
  try:
   for key,label in IONS.items():
    bounds=ions.get(key) or {}
    low=numeric(bounds['min'],label+'下限',0,100000) if bounds.get('min') not in (None,'') else None
    high=numeric(bounds['max'],label+'上限',0,100000) if bounds.get('max') not in (None,'') else None
    if low is not None and high is not None and low>high:raise ValueError(label+'下限不能大于上限。')
    if low is not None or high is not None:count+=1
    ranges[key]={'min':low,'max':high}
   if not count:raise ValueError('请至少填写一项离子的目标范围。')
  except (ValueError,TypeError,KeyError) as exc:raise ApiError('VALIDATION_FORMAT',str(exc),422)
  active=payload.get('active',True) is True;default=payload.get('is_default',False) is True and active
  if default:self.env['brew.water.target'].sudo().search([('company_id','=',self.company.id),('product_tmpl_id','=',product.id),('factory_id','=',factory.id if factory else False),('is_default','=',True)]).write({'is_default':False})
  values={'name':name,'company_id':self.company.id,'product_tmpl_id':product.id,'factory_id':factory.id if factory else False,'active':active,'is_default':default,'ranges_json':json.dumps(ranges,ensure_ascii=False),'notes':str(payload.get('notes') or '')}
  if target:
   history=json.loads(target.history_json or '[]');history.append(self.serialize_target(target));values['history_json']=json.dumps(history,ensure_ascii=False);target.write(values)
  else:target=self.env['brew.water.target'].sudo().create(values)
  return self.serialize_target(target)
 def validate_target(self,water,equipment,product_id=None):
  if not water or not water.get('enabled'):return
  target=water.get('target') or {}
  if not target.get('id'):return
  try:identifier=int(target['id'])
  except (TypeError,ValueError):raise ApiError('VALIDATION_FORMAT','目标方案标识无效。',422)
  record=self.env['brew.water.target'].sudo().with_context(active_test=False).search([('id','=',identifier),('company_id','=',self.company.id)],limit=1)
  if not record or (record.factory_id and record.factory_id!=equipment.brew_factory_id):raise ApiError('DATA_SCOPE_DENIED','目标水质方案不适用于当前酒厂。',403)
