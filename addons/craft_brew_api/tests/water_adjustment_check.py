"""Run run(env) in an Odoo shell. Fixtures are rolled back by the caller."""
import copy
import json
import uuid
from datetime import timedelta
from odoo import Command, fields
from odoo.addons.craft_brew_api.services.water import WaterService
from odoo.addons.craft_brew_api.services.workflow import WorkflowService
from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_workflow.models.water_chemistry import SALTS, water_volumes, stage_snapshot
from odoo.addons.craft_brew_workflow.models.execution_design import batch_design, ready_mash_nodes

def run(env):
 env=env(su=True)
 code='WATER'+uuid.uuid4().hex[:7]
 company=env.company
 factories=[env['brew.factory'].create({'name':code+str(i),'code':code+str(i),'company_id':company.id}) for i in range(2)]
 def user(role,index):
  return env['res.users'].create({'name':code+role+str(index),'login':code+role+str(index),'company_id':company.id,'company_ids':[Command.set([company.id])],'brew_factory_ids':[Command.set([factories[index].id])],'groups_id':[Command.set([env.ref('base.group_user').id,env.ref('craft_brew_core.group_brew_'+role).id])]})
 manager=user('brewery_manager',0);operator=user('operator',0);director=user('production_director',0)
 water_service=WaterService(env,manager,company)
 quality={'ca':30,'mg':5,'na':10,'cl':20,'so4':30,'hco3':80}
 payload={'factory_id':str(factories[0].id),'name':code,'source_type':'treated','measured_on':fields.Date.today().isoformat(),'ions':quality,'ph':7.5,'alkalinity':65.56,'is_default':True}
 source=water_service.save(payload)
 assert source['factory_id']==str(factories[0].id)
 assert all(s['factory_id']==str(factories[0].id) for s in water_service.list()['items'])
 foreign=WaterService(env,director,company).save({**payload,'factory_id':str(factories[1].id),'name':code+'foreign'})
 try:water_service.save({**foreign,'name':'cross-factory'},int(foreign['id']))
 except ApiError as exc:assert exc.status==403
 else:raise AssertionError('cross-factory write permitted')
 try:WaterService(env,operator,company).save(payload)
 except ApiError as exc:assert exc.status==403
 else:raise AssertionError('operator modified source profile')
 assert any(f['id']==str(factories[1].id) for f in WaterService(env,director,company).list()['factories'])
 equipment=env['maintenance.equipment'].create({'name':code,'company_id':company.id,'brew_factory_id':factories[0].id,'brew_equipment_type':'mash','brew_capacity_l':2000})
 tank=env['brew.tank'].create({'name':code,'factory_id':factories[0].id,'capacity_l':2000,'cleaning_status':'clean'})
 product=env['product.template'].create({'name':code,'company_id':company.id,'detailed_type':'product','brew_item_type':'finished_product'})
 recipe=env['brew.recipe'].create({'name':code,'code':code,'company_id':company.id,'product_tmpl_id':product.id,'equipment_id':equipment.id})
 version=env['brew.recipe.version'].create({'recipe_id':recipe.id,'version':1,'base_volume_l':1000,'state':'active'})
 design={'schema_version':2,'brew_volume_l':1000,'brew_equipment_id':str(equipment.id),'fermentables':[{'name':'测试麦芽','quantity':100}],'hops':[],'yeast':[], 'mash':{'water_grain_ratio':3,'water_calculation':'original','sparge_water_liters':[400],'sparge_count':1,'boil_time_min':60,'evaporation_rate':10,'steps':[{'name':'糖化','temperature':65,'duration_min':30}],'workflow':{'execution_mode':'standard','iodine_check_required':False}},'water_adjustment':{'enabled':True,'source':copy.deepcopy(source),'source_mode':'factory','target':{'name':'测试水质方案','ions':{}},'stages':{k:{'additions':{s:{'amount':30 if s=='gypsum' else 0,'purity':100} for s in SALTS},'acid':{'amount_ml':0,'concentration':88,'density':1.21}} for k in ['mash','sparge']}}}
 water_service.validate_recipe(design['water_adjustment'],equipment)
 edited=water_service.save({**source,'ions':{**quality,'ca':40}},int(source['id']))
 assert edited['ions']['ca']==40
 water_service.validate_recipe(design['water_adjustment'],equipment)  # historical source remains valid
 assert design['water_adjustment']['source']['ions']['ca']==30
 tampered=copy.deepcopy(design['water_adjustment']);tampered['source']['ions']['ca']=99
 try:water_service.validate_recipe(tampered,equipment)
 except ApiError:pass
 else:raise AssertionError('unattributed source override accepted')
 scaled=batch_design({'design':design},500)
 assert scaled['water_adjustment']['stages']['mash']['additions']['gypsum']['amount']==15
 assert design['water_adjustment']['stages']['mash']['additions']['gypsum']['amount']==30
 assert stage_snapshot(scaled,'mash')['recipe_stage']['volume_l']==150
 start=fields.Datetime.now()
 plan=env['brew.production.plan'].create({'name':code,'company_id':company.id,'factory_id':factories[0].id,'product_tmpl_id':product.id,'recipe_version_id':version.id,'planned_volume_l':1000,'planned_start':start,'planned_end':start+timedelta(days=14),'equipment_id':equipment.id,'tank_id':tank.id,'state':'in_progress','mash_owner_id':operator.id,'fermentation_owner_id':operator.id,'recipe_snapshot_json':json.dumps({'design':design,'base_volume_l':1000})})
 task=env['brew.task'].create({'name':code,'task_type':'mashing','role_code':'operator','assignee_id':operator.id,'company_id':company.id,'factory_id':factories[0].id,'target_model':plan._name,'target_id':plan.id,'state':'in_progress'})
 nodes=task.mash_execution_checkpoints()
 assert {'water_mash','water_sparge'}<={n['code'] for n in nodes}
 assert next(n for n in nodes if n['code']=='water_mash')['water_adjustment']['source']['ions']['ca']==30
 assert next(n for n in nodes if n['code']=='water_sparge')['parallel_after']=='prepare_sparge_water'
 for node in nodes:
  if node['code']=='water_mash':break
  node.update(state='done',completed_at=start.isoformat(),completed_by=operator.name)
 task.write({'execution_checkpoints_json':json.dumps(nodes)})
 assert 'water_mash' in {n['code'] for n in ready_mash_nodes(nodes)}
 actual={'volume_l':300,'additions':{s:{'amount':30 if s=='gypsum' else 0,'lot':code} for s in SALTS},'acid_ml':0,'measured_ph':6.2}
 workflow=WorkflowService(env,operator,company)
 try:workflow._confirm_mash_checkpoint(task,{'checkpoint_key':'water_mash','water_actual':actual})
 except ApiError:pass
 else:raise AssertionError('missing physical confirmation accepted')
 workflow._confirm_mash_checkpoint(task,{'checkpoint_key':'water_mash','water_actual':actual,'confirmed':True})
 saved=next(n for n in json.loads(task.execution_checkpoints_json) if n['code']=='water_mash')
 assert saved['state']=='done'
 assert saved['water_actual']['measured_ph']==6.2
 assert abs(saved['water_actual']['result']['ions']['ca']-53.278)<1e-6
 assert saved['water_actual']['lots']['gypsum']==code
 assert saved['water_adjustment']['source']['ions']['ca']==30
 no_water=copy.deepcopy(design);no_water.pop('water_adjustment')
 plan.with_context(brew_state_transition=True).write({'recipe_snapshot_json':json.dumps({'design':no_water})})
 task2=task.copy({'name':code+'legacy','execution_checkpoints_json':'[]'})
 assert not any(n['code'].startswith('water_') for n in task2.mash_execution_checkpoints())
 print('PASS: scoped source management, immutable source revisions, source override validation, batch scaling, water task ordering, actual dosing and historical records, disabled legacy flow')
