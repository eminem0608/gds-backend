"""Real database workflow checks. Fixtures and stock moves always roll back."""
import json
import uuid
from datetime import timedelta
from odoo import Command, fields
from odoo.exceptions import ValidationError


def run(env):
    from odoo.addons.craft_brew_api.services.workflow import WorkflowService
    from odoo.addons.craft_brew_api.services.planning import PlanningService
    from odoo.addons.craft_brew_api.services.trace import TraceService
    from odoo.addons.craft_brew_api.services.stock_browser import StockBrowserService
    from odoo.addons.craft_brew_api.services.product_trace import ProductTrace
    from odoo.addons.craft_brew_api.controllers.base import ApiError
    prefix = 'PF' + uuid.uuid4().hex[:8]
    company = env.company
    factory = env['brew.factory'].create({'name': prefix, 'code': prefix, 'company_id': company.id})
    other = env['brew.factory'].create({'name': prefix+'X', 'code': prefix+'X', 'company_id': company.id})
    def actor(suffix, role):
        return env['res.users'].create({'name': prefix+suffix, 'login': prefix+suffix, 'company_id': company.id,
            'company_ids': [Command.set([company.id])], 'brew_factory_ids': [Command.set([factory.id])],
            'groups_id': [Command.set([env.ref('base.group_user').id, env.ref('craft_brew_core.group_brew_'+role).id])]})
    director, operator, warehouse, stranger = actor('D','production_director'), actor('O','operator'), actor('W','warehouse'), actor('S','operator')
    beer = env['product.template'].create({'name': prefix, 'default_code': prefix+'LG', 'company_id':company.id, 'detailed_type':'product', 'tracking':'lot', 'brew_item_type':'finished_product'})
    material = env['product.template'].create({'name':prefix+'瓶', 'company_id':company.id, 'detailed_type':'product', 'tracking':'lot', 'brew_item_type':'packaging_material', 'brew_packaging_type':'bottle', 'brew_packaging_state':'confirmed'})
    package = env['product.packaging'].create({'name':'500mL', 'product_id':beer.product_variant_id.id, 'qty':1, 'brew_volume_l':0.5, 'brew_barcode_level':'bottle', 'company_id':company.id})
    equipment = env['maintenance.equipment'].create({'name':prefix,'company_id':company.id,'brew_factory_id':factory.id,'brew_equipment_type':'mash'})
    tank = env['brew.tank'].create({'name':prefix,'factory_id':factory.id,'capacity_l':1000})
    recipe = env['brew.recipe'].create({'name':prefix,'code':prefix,'company_id':company.id,'product_tmpl_id':beer.id,'equipment_id':equipment.id})
    version = env['brew.recipe.version'].create({'recipe_id':recipe.id,'version':1,'base_volume_l':100,'state':'active'})
    now = fields.Datetime.now()
    pack = {'materials_only':True, 'finished_packaging_id':str(package.id), 'materials':[{'material_id':str(material.id),'product_id':str(material.product_variant_id.id),'name':material.name,'quantity':100,'unit':'个'}], 'planned_start':now.isoformat(), 'planned_end':(now+timedelta(hours=1)).isoformat()}
    plan = env['brew.production.plan'].create({'company_id':company.id,'factory_id':factory.id,'product_tmpl_id':beer.id,'recipe_version_id':version.id,'planned_volume_l':100,'planned_start':now-timedelta(days=3),'planned_end':now-timedelta(days=1),'equipment_id':equipment.id,'tank_id':tank.id,'state':'done','packaging_owner_id':operator.id,'packaging_plan_json':json.dumps(pack),'brewing_completion_json':json.dumps({'actual_volume_l':100,'disposition':'in_tank'})})
    batch = env['brew.production.batch'].create({'production_plan_id':plan.id,'state':'completed','actual_volume_l':100})
    task = env['brew.task'].search([('target_model','=',plan._name),('target_id','=',plan.id),('task_type','=','packaging')],limit=1)
    if not task:
        task = env['brew.task'].create({'name':prefix,'company_id':company.id,'factory_id':factory.id,'task_type':'packaging','target_model':plan._name,'target_id':plan.id,'assignee_id':operator.id})
    def location(name, kind, fac=factory):
        return env['stock.location'].create({'name':prefix+name,'usage':'internal','company_id':company.id,'brew_factory_id':fac.id,'brew_location_type':kind})
    source, target, foreign = location('包材库','packaging'), location('成品库','finished'), location('异厂成品库','finished',other)
    lot = env['stock.lot'].create({'name':prefix,'product_id':material.product_variant_id.id,'company_id':company.id,'brew_quality_status':'qualified'})
    env['stock.quant']._update_available_quantity(material.product_variant_id,source,200,lot_id=lot)
    flow = WorkflowService(env,operator,company)
    planner = PlanningService(env,director,company)
    def action(name, values=None):
        return flow.task_action(task.id,name,{'version':task.write_date.isoformat(),**(values or {})})
    def rejects(call):
        try:
            with env.cr.savepoint(): call()
        except (ApiError,ValidationError): return
        raise AssertionError('Expected operation to be rejected')
    # Historical unverified raw liquid can be reconciled without changing its quantity.
    planner.action(plan.id,'confirm-raw-location',{'version':plan.write_date.isoformat(),'tank_id':tank.id,'confirmed':True})
    assert tank.current_production_batch_id == batch and tank.current_volume_l == 100
    assert planner.progress(plan.id)['packaging_execution_options']['locations'][0]['id']==str(target.id)
    action('packaging-start')
    base={'actual_qty':100,'loss_volume_l':1,'finished_packaging_id':package.id,'finished_location_id':target.id,'actual_materials':[{'material_id':material.id,'quantity':100}],'confirmed':True}
    rejects(lambda: WorkflowService(env,stranger,company).task_action(task.id,'packaging-complete',{'version':task.write_date.isoformat(),**base}))
    rejects(lambda: action('packaging-complete',{**base,'actual_qty':float('nan')}))
    rejects(lambda: action('packaging-complete',{**base,'actual_qty':201}))
    rejects(lambda: action('packaging-complete',{**base,'finished_location_id':foreign.id}))
    rejects(lambda: action('packaging-complete',{**base,'actual_materials':[{'material_id':material.id,'quantity':201}]}))
    lot.brew_frozen=True
    rejects(lambda: action('packaging-complete',base))
    lot.brew_frozen=False
    lot.brew_expiration_date=fields.Date.today()-timedelta(days=1)
    rejects(lambda: action('packaging-complete',base))
    lot.brew_expiration_date=False
    count=env['brew.inventory.count'].create({'company_id':company.id,'location_id':source.id})
    count.action_start()
    rejects(lambda: action('packaging-complete',base))
    count.action_cancel(warehouse,'Fixture resumes stock')
    action('packaging-complete',base)
    order = env['brew.packaging.order'].search([('task_id','=',task.id)])
    assert task.state=='done' and order.state=='pending_release'
    assert tank.current_volume_l==49 and tank.current_production_batch_id==batch
    assert env['stock.quant']._get_available_quantity(material.product_variant_id,source,lot_id=lot)==100
    assert not order.finished_lot_id
    stock=StockBrowserService(env,warehouse,company)
    raw=stock.production_liquids({'factory_id':factory.id,'_plan_id':plan.id})
    assert len(raw)==1 and raw[0]['volume_l']==49,raw
    rejects(lambda: action('packaging-complete',base))
    trace=TraceService(env,warehouse,company)
    rejects(lambda: trace.packaging_action(order.id,'release',{'version':order.write_date.isoformat(),'confirmed':False}))
    receipt_count=env['brew.inventory.count'].create({'company_id':company.id,'location_id':target.id})
    receipt_count.action_start()
    rejects(lambda: trace.packaging_action(order.id,'release',{'version':order.write_date.isoformat(),'confirmed':True}))
    assert order.state=='pending_release' and not order.finished_lot_id
    receipt_count.action_cancel(warehouse,'Fixture resumes receipts')
    trace.packaging_action(order.id,'release',{'version':order.write_date.isoformat(),'confirmed':True})
    assert order.state=='released' and order.finished_lot_id
    assert tank.current_volume_l==49 and tank.current_production_batch_id==batch
    assert env['stock.quant']._get_available_quantity(beer.product_variant_id,target,lot_id=order.finished_lot_id)==100
    assert env['stock.quant']._get_available_quantity(material.product_variant_id,source,lot_id=lot)==100
    rejects(lambda: trace.packaging_action(order.id,'release',{'version':order.write_date.isoformat(),'confirmed':True}))
    result=ProductTrace(trace).detail(batch.id)
    assert result['batch']['finished_product_code']==batch.finished_product_code
    # Remaining liquid can acquire a second task; the same batch code is retained.
    second_pack={**pack,'materials':[{'material_id':material.id,'quantity':96}],'planned_start':(now+timedelta(days=1)).isoformat(),'planned_end':(now+timedelta(days=1,hours=1)).isoformat()}
    planner.action(plan.id,'set-packaging-plan',{'version':plan.write_date.isoformat(),'packaging_owner_id':operator.id,'packaging_plan':second_pack})
    task=env['brew.task'].search([('target_model','=',plan._name),('target_id','=',plan.id),('task_type','=','packaging'),('state','!=','cancelled')],order='id desc',limit=1)
    assert task.state=='pending' and task.id!=order.task_id.id
    action('packaging-start')
    action('packaging-complete',{**base,'actual_qty':96,'loss_volume_l':1,'actual_materials':[{'material_id':material.id,'quantity':96}]})
    second=env['brew.packaging.order'].search([('task_id','=',task.id)])
    assert second.output_volume_l==48 and tank.current_volume_l==0 and not tank.current_production_batch_id
    assert not stock.production_liquids({'factory_id':factory.id,'_plan_id':plan.id})
    # Releasing after the emptied tank is reused cannot empty the next batch's tank.
    next_plan=plan.copy({'packaging_plan_json':'{}','packaging_owner_id':False,'state':'done'})
    next_batch=env['brew.production.batch'].create({'production_plan_id':next_plan.id,'state':'completed','actual_volume_l':20})
    tank.write({'current_production_batch_id':next_batch.id,'current_volume_l':20,'status':'ready'})
    trace.packaging_action(second.id,'release',{'version':second.write_date.isoformat(),'confirmed':True})
    assert tank.current_volume_l==20 and tank.current_production_batch_id==next_batch
    assert env['stock.quant']._get_available_quantity(beer.product_variant_id,target,lot_id=second.finished_lot_id)==96
    assert len(env['brew.packaging.order'].search([('batch_id','=',batch.id)]))==2
    # Old task details use their own output, and cannot target the latest plan JSON.
    old = flow.serialize_task(order.task_id)
    assert old['plan']['packaging_is_current'] is False
    assert old['plan']['packaging_plan']['execution']['actual_qty'] == 100
    rejects(lambda: WorkflowService(env,operator,company).task_action(order.task_id.id,'packaging-register-output',{'version':order.task_id.write_date.isoformat(),**base}))
    env['stock.quant']._update_available_quantity(material.product_variant_id,source,1000,lot_id=lot)
    def fixture(suffix, execution=None):
        case_tank = env['brew.tank'].create({'name':prefix+suffix,'factory_id':factory.id,'capacity_l':1000})
        case_plan = plan.copy({'state':'done','tank_id':case_tank.id,'packaging_plan_json':json.dumps({**pack,**({'execution':execution} if execution else {})})})
        case_batch = env['brew.production.batch'].create({'production_plan_id':case_plan.id,'state':'completed','actual_volume_l':100})
        case_task = env['brew.task'].search([('target_model','=',case_plan._name),('target_id','=',case_plan.id),('task_type','=','packaging')],order='id desc',limit=1)
        if not case_task:
            case_task = env['brew.task'].create({'name':prefix+suffix,'company_id':company.id,'factory_id':factory.id,'task_type':'packaging','target_model':case_plan._name,'target_id':case_plan.id,'assignee_id':operator.id})
        return case_plan,case_batch,case_task,case_tank
    # A legacy pending-release order must deduct only its own quantity, leaving the tank occupied.
    lp,lb,lt,lta=fixture('LegacyPending')
    planner.action(lp.id,'confirm-raw-location',{'version':lp.write_date.isoformat(),'tank_id':lta.id,'confirmed':True})
    legacy=env['brew.packaging.order'].create({'session_id':lb.fermentation_session_id.id,'product_id':beer.product_variant_id.id,'packaging_id':package.id,'finished_location_id':target.id,'planned_volume_l':100})
    legacy.write({'state':'pending_release','output_qty':100,'output_volume_l':50,'loss_volume_l':1})
    trace.packaging_action(legacy.id,'release',{'version':legacy.write_date.isoformat(),'confirmed':True})
    assert lta.current_volume_l==49 and lta.current_production_batch_id==lb and legacy.liquid_deducted
    # Historical material-only completions register output and deduct liquid once.
    rp,rb,rt,rta=fixture('RepairMaterials')
    planner.action(rp.id,'confirm-raw-location',{'version':rp.write_date.isoformat(),'tank_id':rta.id,'confirmed':True})
    rt.write({'state':'done'})
    flow.task_action(rt.id,'packaging-register-output',{'version':rt.write_date.isoformat(),**base})
    assert rta.current_volume_l==49
    # Historical output already counted in the raw-liquid ledger must not be deducted again.
    history={'actual_qty':100,'output_volume_l':50,'loss_volume_l':1,'completed_at':now.isoformat(),'materials':[{'material_id':str(material.id),'quantity':100}]}
    hp,hb,ht,hta=fixture('RepairOutput',history)
    planner.action(hp.id,'confirm-raw-location',{'version':hp.write_date.isoformat(),'tank_id':hta.id,'confirmed':True})
    assert hta.current_volume_l==49
    ht.write({'state':'done'})
    flow.task_action(ht.id,'packaging-register-output',{'version':ht.write_date.isoformat(),**base})
    assert hta.current_volume_l==49
    assert stock.production_liquids({'factory_id':factory.id,'_plan_id':hp.id})[0]['volume_l']==49
    # A fully consumed historical session can register missing output after its tank is reused.
    full_history={**history,'actual_qty':200,'output_volume_l':100,'loss_volume_l':0}
    fp,fb,ft,fta=fixture('RepairEmpty',full_history)
    fs=env['brew.fermentation.session'].create_from_batch(fb)
    fb.fermentation_session_id=fs
    fs.write({'state':'completed','current_volume_l':0})
    fta.write({'current_production_batch_id':next_batch.id,'current_volume_l':20,'status':'ready'})
    ft.write({'state':'done'})
    flow.task_action(ft.id,'packaging-register-output',{'version':ft.write_date.isoformat(),**base,'actual_qty':200,'loss_volume_l':0})
    assert fta.current_volume_l==20 and fta.current_production_batch_id==next_batch
    # Packaging specification snapshots control base-unit receipts after the spec changes.
    mp,mb,mt,mta=fixture('MultiUnit')
    planner.action(mp.id,'confirm-raw-location',{'version':mp.write_date.isoformat(),'tank_id':mta.id,'confirmed':True})
    box=package.copy({'name':'12瓶一箱','qty':12,'brew_volume_l':6})
    flow.task_action(mt.id,'packaging-start',{'version':mt.write_date.isoformat()})
    flow.task_action(mt.id,'packaging-complete',{'version':mt.write_date.isoformat(),**base,'actual_qty':2,'finished_packaging_id':box.id})
    mo=env['brew.packaging.order'].search([('task_id','=',mt.id)])
    box.write({'qty':24,'brew_volume_l':12})
    trace.packaging_action(mo.id,'release',{'version':mo.write_date.isoformat(),'confirmed':True})
    assert mta.current_volume_l==87
    assert env['stock.quant']._get_available_quantity(beer.product_variant_id,target,lot_id=mo.finished_lot_id)==24
    # Operator reports quantity and liquid volume independently, without selecting a spec.
    dp,db,dt,dta=fixture('DirectOutput')
    planner.action(dp.id,'confirm-raw-location',{'version':dp.write_date.isoformat(),'tank_id':dta.id,'confirmed':True})
    flow.task_action(dt.id,'packaging-start',{'version':dt.write_date.isoformat()})
    direct={**base,'actual_qty':7,'output_volume_l':13,'loss_volume_l':2,'finished_product_name':'手工登记成品','output_unit':'box','actual_materials':[{'material_id':material.id,'quantity':7}]}
    direct.pop('finished_packaging_id')
    rejects(lambda: flow.task_action(dt.id,'packaging-complete',{'version':dt.write_date.isoformat(),**direct,'output_volume_l':float('nan')}))
    rejects(lambda: flow.task_action(dt.id,'packaging-complete',{'version':dt.write_date.isoformat(),**direct,'output_volume_l':100}))
    rejects(lambda: flow.task_action(dt.id,'packaging-complete',{'version':dt.write_date.isoformat(),**direct,'output_unit':'invalid'}))
    flow.task_action(dt.id,'packaging-complete',{'version':dt.write_date.isoformat(),**direct})
    do=env['brew.packaging.order'].search([('task_id','=',dt.id)])
    assert do.output_qty==7 and do.output_volume_l==13 and do.loss_volume_l==2
    assert dta.current_volume_l==85 and not do.packaging_id.active
    trace.packaging_action(do.id,'release',{'version':do.write_date.isoformat(),'confirmed':True})
    assert env['stock.quant']._get_available_quantity(beer.product_variant_id,target,lot_id=do.finished_lot_id)==7
    assert dta.current_volume_l==85
    print('PASS packaging completion → pending release → finished stock; exact raw/material deduction; partial and repeated packaging; reused tank; invalid quantities, foreign scope, assignee and duplicate guards; frozen/expired materials and stock-count pauses; historical repair; legacy release; packaging unit snapshots')

try:
    run(env)
finally:
    env.cr.rollback()
