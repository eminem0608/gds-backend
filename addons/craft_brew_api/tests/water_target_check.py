"""Target-library permissions and snapshots. Caller must roll back fixtures."""
import copy
import uuid
from odoo import Command
from odoo.addons.craft_brew_api.services.water import WaterService
from odoo.addons.craft_brew_api.controllers.base import ApiError

def run(env):
 env=env(su=True);company=env.company;code='WT'+uuid.uuid4().hex[:8]
 factories=[env['brew.factory'].create({'name':code+str(i),'code':code+str(i),'company_id':company.id}) for i in range(2)]
 def user(role,index):return env['res.users'].create({'name':code+role+str(index),'login':code+role+str(index),'company_id':company.id,'company_ids':[Command.set([company.id])],'brew_factory_ids':[Command.set([factories[index].id])],'groups_id':[Command.set([env.ref('base.group_user').id,env.ref('craft_brew_core.group_brew_'+role).id])]})
 local=WaterService(env,user('brewery_manager',0),company);hq=WaterService(env,user('production_director',0),company);operator=WaterService(env,user('operator',0),company)
 products=[env['product.template'].create({'name':code+str(i),'company_id':company.id,'detailed_type':'product','brew_item_type':'finished_product'}) for i in range(2)]
 payload={'product_id':str(products[0].id),'name':code,'factory_id':'','ions':{'ca':{'min':30,'max':80},'mg':{'min':0,'max':15}},'is_default':True}
 common=hq.save_target(payload);assert common['factory_id']==''
 scoped=local.save_target({**payload,'factory_id':str(factories[0].id),'name':code+'local'})
 foreign=hq.save_target({**payload,'factory_id':str(factories[1].id),'name':code+'foreign'})
 rows=local.targets(products[0].id)['items'];assert {r['id'] for r in rows}=={common['id'],scoped['id']}
 assert not next(r for r in rows if r['id']==common['id'])['can_edit']
 assert next(r for r in rows if r['id']==scoped['id'])['can_edit']
 for service,data,identifier in [(local,{**common,'name':'forbidden'},int(common['id'])),(local,{**foreign,'name':'forbidden'},int(foreign['id'])),(local,payload,None),(operator,{**payload,'factory_id':str(factories[0].id)},None)]:
  try:service.save_target(data,identifier)
  except ApiError as exc:assert exc.status==403
  else:raise AssertionError('unauthorized target write permitted')
 try:local.targets(products[0].id,factories[1].id)
 except ApiError as exc:assert exc.status==403
 else:raise AssertionError('foreign target listing permitted')
 snapshot=copy.deepcopy(scoped)
 edited=local.save_target({**scoped,'ions':{'ca':{'min':40,'max':100}}},int(scoped['id']))
 assert snapshot['ions']['ca']['min']==30 and edited['ions']['ca']['min']==40
 assert len(__import__('json').loads(env['brew.water.target'].browse(int(scoped['id'])).history_json))==1
 equipment=env['maintenance.equipment'].create({'name':code,'company_id':company.id,'brew_factory_id':factories[0].id,'brew_equipment_type':'mash'})
 hq.validate_target({'enabled':True,'target':snapshot},equipment,products[0].id)
 hq.validate_target({'enabled':True,'target':snapshot},equipment,products[1].id)
 try:hq.validate_target({'enabled':True,'target':foreign},equipment,products[0].id)
 except ApiError as exc:assert exc.status==403
 else:raise AssertionError('foreign-factory target applied')
 next_target=local.save_target({**payload,'name':code+'new-default','factory_id':str(factories[0].id)})
 defaults=[r for r in local.targets(products[0].id)['items'] if r['is_default'] and r['factory_id']==str(factories[0].id)]
 assert len(defaults)==1 and defaults[0]['id']==next_target['id']
 stopped=local.save_target({**next_target,'active':False},int(next_target['id']));assert not stopped['is_default']
 for ions in [{'ca':{'min':90,'max':20}},{'ca':{'min':-1}},{}]:
  try:hq.save_target({**payload,'ions':ions,'is_default':False})
  except ApiError as exc:assert exc.status==422
  else:raise AssertionError('invalid target range saved')
 try:local.save_target({**edited,'version':'stale'},int(edited['id']))
 except ApiError as exc:assert exc.status==409
 else:raise AssertionError('stale target write permitted')
 print('PASS: target CRUD, local/HQ scopes, cross-product recipe reuse, default switching, archive behavior, immutable snapshots, range validation and concurrent-edit protection')
