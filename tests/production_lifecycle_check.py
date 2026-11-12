"""Roll-back-only integration checks for administrative closing and scoped reports."""
import json,uuid
from datetime import timedelta
from odoo import fields,Command

def run(env):
 from odoo.addons.craft_brew_api.services.planning import PlanningService
 from odoo.addons.craft_brew_api.services.demand import DemandService
 from odoo.addons.craft_brew_api.services.report_data import ReportDataService
 from odoo.addons.craft_brew_api.services.stock_browser import StockBrowserService
 from odoo.addons.craft_brew_api.controllers.base import ApiError
 code='LC'+uuid.uuid4().hex[:5];company=env.company;admin=env.ref('base.user_admin')
 admin.write({'groups_id':[Command.link(env.ref('craft_brew_core.group_brew_admin').id)]})
 factory=env['brew.factory'].create({'name':code,'code':code,'company_id':company.id})
 other=env['brew.factory'].create({'name':code+'X','code':code+'X','company_id':company.id})
 beer=env['product.template'].create({'name':code,'company_id':company.id,'brew_item_type':'finished_product'})
 equipment=env['maintenance.equipment'].create({'name':code,'company_id':company.id,'brew_factory_id':factory.id,'brew_equipment_type':'mash'})
 tank=env['brew.tank'].create({'name':code,'factory_id':factory.id,'capacity_l':2000})
 from odoo.addons.craft_brew_api.services.master_data import MasterDataService
 master=MasterDataService(env,admin,company)
 location=master._ensure_tank_location(tank)
 assert location.brew_location_type=='semi_finished' and location.brew_factory_id==factory
 assert master._ensure_tank_location(tank)==location and tank.current_volume_l==0
 automatic,_=master.create('tanks',{'name':code+'auto','factory_id':factory.id,'capacity_l':1000})
 assert automatic.stock_location_id and automatic.stock_location_id!=location
 recipe=env['brew.recipe'].create({'name':code,'code':code,'company_id':company.id,'product_tmpl_id':beer.id,'equipment_id':equipment.id})
 version=env['brew.recipe.version'].create({'recipe_id':recipe.id,'version':1,'base_volume_l':200,'state':'active'})
 def user(role):
  return env['res.users'].create({'name':code+role,'login':code+role,'company_id':company.id,'company_ids':[Command.set([company.id])],'brew_factory_ids':[Command.set([factory.id])],'groups_id':[Command.set([env.ref('base.group_user').id,env.ref('craft_brew_core.group_brew_'+role).id])]})
 director,manager,gm,sales,packer=map(user,['production_director','brewery_manager','general_manager','sales','operator'])
 def plan(state='approved',demand=None):
  now=fields.Datetime.now()
  return env['brew.production.plan'].create({'company_id':company.id,'factory_id':factory.id,'demand_id':demand.id if demand else False,'product_tmpl_id':beer.id,'recipe_version_id':version.id,'planned_volume_l':200,'planned_start':now,'planned_end':now+timedelta(days=2),'equipment_id':equipment.id,'tank_id':tank.id,'state':state})
 def payload(p):return {'version':p.write_date.isoformat(),'reason':'回滚测试'}
 def rejects(fn,code):
  try:
   with env.cr.savepoint():fn()
  except ApiError as e:assert e.code==code,(e.code,code)
  else:raise AssertionError('Expected '+code)
 p=plan();service=PlanningService(env,director,company)
 rejects(lambda:PlanningService(env,manager,company).action(p.id,'delete',payload(p)),'PERMISSION_DENIED')
 rejects(lambda:service.action(p.id,'cancel',{'version':p.write_date.isoformat()}),'VALIDATION_REQUIRED')
 rejects(lambda:service.action(p.id,'cancel',{'version':'stale','reason':'x'}),'VERSION_CONFLICT')
 task=env['brew.task'].create({'name':code,'task_type':'mashing','company_id':company.id,'factory_id':factory.id,'role_code':'production_director','target_model':p._name,'target_id':p.id})
 service.action(p.id,'cancel',payload(p));assert p.state=='cancelled' and task.state=='cancelled' and p.closed_by_id==director
 service.action(p.id,'delete',payload(p));assert p.deleted_at and p.exists()
 assert p.id not in [int(r['id']) for r in service.list(1,100)[0]]
 rejects(lambda:service.action(p.id,'dispatch',payload(p)),'STATE_PRECONDITION_FAILED')
 d=env['brew.product.demand'].create({'company_id':company.id,'salesperson_id':admin.id,'product_tmpl_id':beer.id,'requested_volume_l':200,'requested_delivery_date':fields.Date.today(),'state':'planned'})
 p=plan('in_progress',d)
 batch=env['brew.production.batch'].create({'production_plan_id':p.id,'state':'in_tank','actual_volume_l':200,'entered_tank_at':fields.Datetime.now()})
 tank.write({'status':'ready','current_volume_l':200,'current_production_batch_id':batch.id,'current_batch_no':batch.name})
 p.brewing_completion_json=json.dumps({'actual_volume_l':200,'loss_volume_l':0,'disposition':'in_tank'})

 # Fermentation completion must retain physical tank occupancy, even on the task-only workflow.
 from odoo.addons.craft_brew_api.services.workflow import WorkflowService
 for kind in ['mashing','fermentation']:
  env['brew.task'].create({'name':code+kind,'task_type':kind,'company_id':company.id,'factory_id':factory.id,'role_code':'operator','target_model':p._name,'target_id':p.id,'state':'done'})
 tank.write({'status':'empty','current_volume_l':0,'current_production_batch_id':False})
 assert WorkflowService(env,admin,company).complete_fermentation_plan(p)
 assert tank.current_volume_l==200 and tank.current_production_batch_id==batch and tank.status=='ready'
 customer=env['res.partner'].create({'name':code+'Customer','company_id':company.id,'customer_rank':1})
 PlanningService(env,admin,company).action(p.id,'set-raw-customer',{**payload(p),'customer_id':customer.id,'customer_reference':'Order-1'})
 assert json.loads(p.packaging_plan_json)['customer_id']==str(customer.id)
 package=env['product.packaging'].create({'name':code+'500ml','product_id':beer.product_variant_id.id,'qty':1,'brew_volume_l':0.5,'brew_barcode_level':'bottle','company_id':company.id})
 now=fields.Datetime.now()
 PlanningService(env,admin,company).action(p.id,'set-packaging-plan',{**payload(p),'packaging_owner_id':packer.id,'packaging_plan':{'packaging_id':package.id,'planned_qty':100,'planned_start':(now+timedelta(hours=1)).isoformat(),'planned_end':(now+timedelta(hours=2)).isoformat()}})
 assert json.loads(p.packaging_plan_json)['customer_name']==customer.name
 assert env['brew.task'].search_count([('target_model','=',p._name),('target_id','=',p.id),('task_type','=','packaging'),('state','!=','cancelled')])==1
 assert p.state=='done' and tank.current_volume_l==200
 # Historical missing tank links surface as unverified, without changing physical records.
 from odoo.addons.craft_brew_api.services.reporting import ReportingService
 tank.write({'status':'empty','current_volume_l':0,'current_production_batch_id':False})
 dashboard=ReportingService(env,manager,company).dashboard(factory.id)
 assert dashboard['metrics']['ready_volume_l']==200 and dashboard['metrics']['raw_location_pending']==1
 assert next(t for t in dashboard['tanks'] if t['id']==str(tank.id))['stage_label']=='原液待核对'
 from odoo.addons.craft_brew_api.services.fermentation import FermentationService
 unverified=FermentationService(env,manager,company).tank_detail(tank.id)
 assert unverified['stage']=='location_pending' and unverified['location_checks'][0]['volume_l']==200
 assert unverified['location_checks'][0]['batch_name']==batch.name
 assert tank.current_volume_l==0 and not tank.current_production_batch_id
 tank.write({'status':'ready','current_volume_l':200,'current_production_batch_id':batch.id})
 # Names, rather than warehouse paths, are shared by report previews and CSV.
 root=env['stock.location'].create({'name':'L1','usage':'view','company_id':company.id})
 loc=env['stock.location'].create({'name':'领料暂存区','usage':'internal','company_id':company.id,'location_id':root.id,'brew_factory_id':factory.id})
 assert ReportDataService._location_name(loc)=='领料暂存区'
 reports=ReportDataService(env,manager,company)
 assert 'purchase' in [r['code'] for r in reports.catalog()['items']]
 rejects(lambda:reports.data('production',{'factory_id':other.id}),'DATA_SCOPE_DENIED')
 rejects(lambda:reports.csv('purchase',{'factory_id':other.id}),'DATA_SCOPE_DENIED')
 rejects(lambda:ReportDataService(env,sales,company).data('production',{}),'PERMISSION_DENIED')
 rejects(lambda:ReportDataService(env,director,company).data('purchase',{}),'PERMISSION_DENIED')
 rows=reports.data('liquid',{'factory_id':factory.id})['rows'];assert any(r['batch']==batch.name and r['quantity']==200 for r in rows)
 DemandService(env,gm,company).action(d.id,'delete',payload(d))
 assert d.deleted_at and d.state=='cancelled' and p.state=='terminated'
 assert tank.current_volume_l==200 and tank.current_production_batch_id==batch and batch.state=='completed'
 rows=StockBrowserService(env,manager,company).production_liquids({'factory_id':factory.id})
 assert any(r['batch_id']==str(batch.id) and r['on_hand']==200 for r in rows),'Closing lost physical liquid'
 assert d.id not in [int(r['id']) for r in DemandService(env,gm,company).list(1,100)[0]]
 title,content=reports.csv('production',{'factory_id':factory.id});assert content.startswith(b'\xef\xbb\xbf') and '已删除'.encode() in content # historical deleted plan remains auditable
 assert '已终止'.encode() in content
 # Packaging consumes only this batch's physical volume and leaves the remainder in the tank.
 p2=plan('done')
 b2=env['brew.production.batch'].create({'production_plan_id':p2.id,'state':'completed','actual_volume_l':200,'entered_tank_at':fields.Datetime.now()})
 p2.brewing_completion_json=json.dumps({'actual_volume_l':200,'disposition':'in_tank'})
 p2.packaging_plan_json=json.dumps({'packaging_id':str(package.id),'unit_volume_l':0.5,'planned_qty':100})
 tank.write({'status':'ready','current_volume_l':200,'current_production_batch_id':b2.id})
 pt=env['brew.task'].create({'name':code+'pack','task_type':'packaging','company_id':company.id,'factory_id':factory.id,'role_code':'operator','assignee_id':packer.id,'target_model':p2._name,'target_id':p2.id,'state':'in_progress'})
 finished=env['stock.location'].create({'name':code+'成品库','usage':'internal','company_id':company.id,'brew_factory_id':factory.id,'brew_location_type':'finished'})
 WorkflowService(env,packer,company).task_action(pt.id,'packaging-complete',{'version':pt.write_date.isoformat(),'confirmed':True,'actual_qty':100,'loss_volume_l':2,'finished_location_id':finished.id})
 assert tank.current_volume_l==148 and tank.current_production_batch_id==b2 and tank.status=='ready'
 assert StockBrowserService(env,manager,company).production_liquids({'_plan_id':p2.id})[0]['on_hand']==148
 assert all(c in reports.catalog()['items'][0]['columns'][0] for c in ['key','label'])
 # A tank's lifetime totals must not be limited to the six recent rows or use planned volumes.
 from odoo.addons.craft_brew_api.services.fermentation import FermentationService
 tank_service=FermentationService(env,manager,company)
 before=tank_service.tank_detail(tank.id)['history_summary']
 for index in range(8):
  history_plan=plan('done')
  env['brew.production.batch'].create({'production_plan_id':history_plan.id,'state':'completed',
   'actual_volume_l':100+index,'completed_at':now-timedelta(days=8-index)})
 tank_data=tank_service.tank_detail(tank.id)
 assert len(tank_data['history'])==6
 assert tank_data['history_summary']['completed_batches']==before['completed_batches']+8
 assert tank_data['history_summary']['actual_volume_l']==before['actual_volume_l']+sum(range(100,108))
 assert tank_data['history_summary']['last_completed_at']
 assert tank.current_volume_l==148 and tank.current_production_batch_id==b2
 print('PASS: tank lifetime actual production totals, recent-history limit independent of totals, physical balance unchanged')
 print('PASS: auto brewing retains tank, post-brewing packaging/customer assignment, location names; director/general-manager authority, denied factory role, approved cancellation, deletion retains records, cascade closure, stale-version checks, in-tank volume preserved, report catalog/factory scope/export CSV')

if __name__=='__main__':
 import odoo
 from odoo import api,SUPERUSER_ID
 odoo.tools.config.parse_config(['-c','/etc/odoo/odoo.conf','-d','craft_brew_dev','--no-http'])
 with odoo.registry('craft_brew_dev').cursor() as cr:
  try:run(api.Environment(cr,SUPERUSER_ID,{}))
  finally:cr.rollback()
