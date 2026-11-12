from odoo import http
from odoo.http import request
from .base import BrewApiController
from ..services.stock_browser import StockBrowserService

class StockBrowserController(BrewApiController):
 @http.route('/api/v1/inventory/products/<int:product_id>/image',type='http',auth='public',methods=['GET'],csrf=False)
 def product_image(self,product_id):
  rid=self._request_id()
  try:
   user,company,session,mode=self._authenticate();service=StockBrowserService(request.env,user,company)
   content,mimetype=service.product_image(product_id)
   return request.make_response(content,headers=[('Content-Type',mimetype),('Cache-Control','private, max-age=300'),('X-Request-ID',rid)])
  except Exception as exc:return self._error_response(exc,rid)

 @http.route('/api/v1/inventory/opening-stock/options',type='http',auth='public',methods=['GET'],csrf=False)
 def opening_stock_options(self):
  rid=self._request_id()
  try:
   user,company,session,mode=self._authenticate();service=StockBrowserService(request.env,user,company)
   return self._response(service.entry_options(request.httprequest.args.get('factory_id')),request_id=rid)
  except Exception as exc:return self._error_response(exc,rid)

 @http.route(['/api/v1/inventory/browse','/api/v1/inventory/browse/<int:product_id>'],type='http',auth='public',methods=['GET'],csrf=False)
 def browse(self,product_id=None):
  rid=self._request_id()
  try:
   user,company,session,mode=self._authenticate();service=StockBrowserService(request.env,user,company)
   args=request.httprequest.args
   if product_id:data=service.detail(product_id,args.get('factory_id'),max(1,int(args.get('history_page',1))))
   else:data=service.list({k:args.get(k) for k in ['factory_id','category','keyword']},max(1,int(args.get('page',1))),min(100,max(1,int(args.get('page_size',30)))))
   return self._response(data,request_id=rid)
  except Exception as exc:return self._error_response(exc,rid)
