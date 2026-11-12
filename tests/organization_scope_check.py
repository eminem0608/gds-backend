"""Rollback-only checks for organization boundaries, including privileged local users."""
import uuid
from odoo import Command

def run(env):
 from odoo.addons.craft_brew_api.services.account_access import AccountAccess
 from odoo.addons.craft_brew_api.services.inventory import InventoryService
 from odoo.addons.craft_brew_api.services.purchase import PurchaseService
 from odoo.addons.craft_brew_api.services.master_data import MasterDataService
 from odoo.addons.craft_brew_api.services.workflow import WorkflowService
 from odoo.addons.craft_brew_api.services.water import WaterService
 from odoo.addons.craft_brew_api.services.report_data import ReportDataService
 from odoo.addons.craft_brew_api.controllers.base import ApiError
 company=env.company;code='OS'+uuid.uuid4().hex[:7]
 def rejects(fn):
  try:
   with env.cr.savepoint(): fn()
  except ApiError as e: assert e.code in {'DATA_SCOPE_DENIED','PERMISSION_DENIED'},e.code
  else: raise AssertionError('Cross-scope operation was allowed')
 fa,fb=[env['brew.factory'].create({'name':code+x,'code':code+x,'company_id':company.id}) for x in ['A','B']]
 root=env['brew.organization'].create({'name':code+'总部','code':code+'HQ','organization_type':'company','company_id':company.id})
 oa,ob=[env['brew.organization'].create({'name':f.name,'code':f.code+'ORG','organization_type':'factory','company_id':company.id,'parent_id':root.id,'factory_id':f.id}) for f in [fa,fb]]
 dept=env['brew.organization'].create({'name':code+'生产部','code':code+'DEP','organization_type':'department','company_id':company.id,'parent_id':oa.id})
 def user(name,role,organization):
  return env['res.users'].create({'name':code+name,'login':code+name,'company_id':company.id,'company_ids':[Command.set([company.id])],'brew_organization_id':organization.id,'brew_direct_role_codes':[role],'brew_factory_ids':[Command.set([fa.id,fb.id])],'groups_id':[Command.set([env.ref('base.group_user').id,env.ref('craft_brew_core.group_brew_'+{'tenant_admin':'admin','operator':'operator','brewery_manager':'brewery_manager'}[role]).id])]})
 hq=user('HQ','tenant_admin',root);local=user('LOCAL','tenant_admin',dept);manager=user('MGR','brewery_manager',oa);worker=user('WORK','operator',dept);other=user('OTHER','operator',ob)
 assert hq.brew_has_headquarters_scope(company) and fa in hq.brew_allowed_factories(company) and fb in hq.brew_allowed_factories(company)
 assert not local.brew_has_headquarters_scope(company)
 for actor in [local,manager,worker]: assert actor.brew_allowed_factories(company)==fa
 assert local.brew_context_payload()['data_scope']['factory_ids']==[str(fa.id)]
 local_policy=AccountAccess(env,local,company);manager_policy=AccountAccess(env,manager,company)
 assert manager_policy.can_manage and not manager_policy.can_configure
 assert set(local_policy.organizations().ids)=={oa.id,dept.id}
 assert local_policy.visible_users(hq|local|worker|other)==local|worker
 assert local_policy.manageable(worker) and not local_policy.manageable(other) and not local_policy.manageable(hq)
 rejects(lambda:local_policy.user_values({'factory_ids':[fb.id]},target=worker,organization=dept))
 rejects(lambda:local_policy.user_values({'role_codes':['tenant_admin']},target=worker,organization=dept))
 rejects(lambda:local_policy.user_values({},target=worker,organization=ob))
 rejects(lambda:local_policy.validate_organization(dept,parent=ob,new_type='department'))
 rejects(lambda:local_policy.validate_organization(oa,new_type='factory'))
 values=manager_policy.user_values({'role_codes':['operator']},target=worker,organization=dept)
 assert values['brew_default_factory_id']==fa.id
 assert InventoryService(env,local,company,enforce_access=False)._allowed_factory_ids()==fa.ids
 assert PurchaseService(env,local,company)._factory_ids()==fa.ids
 master=MasterDataService(env,local,company)
 assert env['brew.factory'].sudo().search(master._domain('factories'))==fa
 warehouse,data=MasterDataService(env,manager,company).create('warehouses',{'name':code+'半成品库','code':code[:5]+'S','brew_factory_id':fa.id,'brew_warehouse_type':'semi_finished'})
 assert warehouse.brew_factory_id==fa and warehouse.lot_stock_id.brew_location_type=='semi_finished'
 rejects(lambda:master.create('warehouses',{'name':code+'BAD','code':code[:5]+'X','brew_factory_id':fb.id,'brew_warehouse_type':'semi_finished'}))
 workflow=WorkflowService(env,local,company)
 tasks=[]
 for f in [fa,fb]:tasks.append(env['brew.task'].create({'name':code+f.name,'task_type':'demand_review','company_id':company.id,'factory_id':f.id,'role_code':'tenant_admin','target_model':'seed.dev.data','target_id':0}))
 ids=[r['id'] for r in workflow.list_tasks(1,1000)[0]]
 assert str(tasks[0].id) in ids and str(tasks[1].id) not in ids
 rejects(lambda:workflow._task(tasks[1].id))
 assert WaterService(env,local,company).can_manage() and WaterService(env,local,company).factories()==fa
 assert ReportDataService(env,local,company).catalog()['scope_kind']=='factory'
 rejects(lambda:ReportDataService(env,local,company).data('liquid',{'factory_id':fb.id}))
 # Deactivating an ancestor removes business access without changing physical stock.
 dept.active=False
 assert not local.brew_allowed_factories(company)
 print('PASS: organization-derived scope, HQ cross-factory access, local admin/manager isolation, account and role restrictions, task/report guards, local semi-finished warehouse creation')
