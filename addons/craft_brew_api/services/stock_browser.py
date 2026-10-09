import json
from odoo import fields
from .inventory import InventoryService
from ..controllers.base import ApiError

class StockBrowserService(InventoryService):
 def _product(self,product_id):
  product=self.env['product.product'].sudo().with_context(active_test=False).search([('id','=',int(product_id)), '|', ('company_id','=',False),('company_id','=',self.company.id)],limit=1)
  if not product: raise ApiError('RESOURCE_NOT_FOUND','物料不存在。',404)
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
  return {'id':str(p.id),'name':p.display_name,'code':p.default_code,'category':self.category(t),'specification':t.brew_specification,'unit':(t.brew_packaging_details or {}).get('unit') or p.uom_id.name,'supplier':{'id':str(t.brew_supplier_id.id),'name':t.brew_supplier_id.name} if t.brew_supplier_id else None,'beer_type':' / '.join(t.brew_beer_category_id.mapped('name')+t.brew_beer_subcategory_id.mapped('name')),'packaging_type':t.brew_packaging_type,'packaging_details':{k:v for k,v in (t.brew_packaging_details or {}).items() if k not in ['artwork_data']}}
 def balances(self,quants,reservations):
  result={'on_hand':0,'reserved':0,'available':0,'blocked':0};today=fields.Date.today()
  buckets={}
  for q in quants:
   key=(q.location_id.id,q.product_id.id,q.lot_id.id)
   b=buckets.setdefault(key,{'quantity':0,'reserved':reservations.get(key,0),'lot':q.lot_id})
   b['quantity']+=q.quantity;b['reserved']+=q.reserved_quantity
  for b in buckets.values():
   lot=b['lot'];qty=b['quantity'];reserved=b['reserved']
   qualified=bool(lot and lot.brew_quality_status=='qualified' and not lot.brew_frozen and (not lot.brew_expiration_date or lot.brew_expiration_date>=today))
   if not lot and quants and quants[0].product_id.tracking=='none':qualified=True
   result['on_hand']+=qty;result['reserved']+=reserved
   result['available']+=max(qty-reserved,0) if qualified else 0
   result['blocked']+=max(qty,0) if not qualified else 0
  return result
 def list(self,filters,page,size):
  quants=self.quants(filters.get('factory_id'));res=self._reservation_totals(quants.mapped('location_id').ids) if quants else {}
  grouped={}
  for q in quants:
   grouped.setdefault((q.location_id.brew_factory_id.id,q.product_id.id),self.env['stock.quant'].sudo().browse())
   grouped[(q.location_id.brew_factory_id.id,q.product_id.id)]|=q
  rows=[];keyword=str(filters.get('keyword') or '').lower()
  for (fid,pid),qs in grouped.items():
   p=qs[0].product_id;info=self.product_info(p)
   if filters.get('category') and info['category']!=filters['category']:continue
   if keyword and keyword not in ' '.join([info['name'],info['code'] or '',info['specification'] or '',qs[0].location_id.brew_factory_id.name]+qs.mapped('lot_id.name')).lower():continue
   dates=qs.mapped('lot_id.brew_expiration_date');dates=[d for d in dates if d]
   balance=self.balances(qs,res)
   rows.append({'product':info,'factory':{'id':str(fid),'name':qs[0].location_id.brew_factory_id.name},**balance,'lot_count':len(qs.mapped('lot_id')),'location_count':len(qs.mapped('location_id')),'earliest_expiration':min(dates).isoformat() if dates else None})
  rows.sort(key=lambda r:(r['factory']['name'],r['product']['name']))
  factories=self.env['brew.factory'].sudo().browse(self._allowed_factory_ids())
  units={}
  for row in rows:
   u=units.setdefault(row['product']['unit'],{'unit':row['product']['unit'],'on_hand':0,'available':0,'reserved':0})
   for k in ['on_hand','available','reserved']:u[k]+=row[k]
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
