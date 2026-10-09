from odoo import http
from odoo.http import request
from .base import BrewApiController
from ..services.packaging import PackagingService

class PackagingController(BrewApiController):
 def context(self,mutation=False):
  user,company,session,mode=self._authenticate()
  if mutation:self._validate_cookie_csrf(mode)
  return user,company,PackagingService(request.env,user,company)
 @http.route('/api/v1/packaging-materials/schema',type='http',auth='public',methods=['GET'],csrf=False)
 def schema(self):
  rid=self._request_id()
  try:return self._response(self.context()[2].schema(),request_id=rid)
  except Exception as exc:return self._error_response(exc,rid)
 @http.route(['/api/v1/packaging-materials','/api/v1/packaging-materials/<int:record_id>','/api/v1/packaging-materials/<int:record_id>/<string:action>'],type='http',auth='public',methods=['GET','POST','PATCH'],csrf=False)
 def materials(self,record_id=None,action=None):
  rid=self._request_id()
  try:
   mutation=request.httprequest.method!='GET';user,company,service=self.context(mutation)
   if not mutation:
    if record_id:return self._response(service.serialize(service.record(record_id)),request_id=rid)
    page=max(1,int(request.httprequest.args.get('page',1)));size=min(100,max(1,int(request.httprequest.args.get('page_size',30))))
    return self._response(service.list(page,size,request.httprequest.args.get('keyword',''),request.httprequest.args.get('include_inactive')=='true'),request_id=rid)
   payload=self._json_body();idem,replay=self._start_idempotency(user,company,payload)
   if replay:return replay
   record,data=service.action(record_id,action,payload) if action else service.save(payload,record_id)
   self._audit('packaging_material',action or 'save',rid,user,company,{'id':record.id})
   return self._complete_idempotency(idem,data)
  except Exception as exc:return self._error_response(exc,rid)
