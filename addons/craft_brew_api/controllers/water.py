from odoo import http
from odoo.http import request
from .base import BrewApiController, ApiError
from ..services.water import WaterService
from odoo.addons.craft_brew_workflow.models.water_chemistry import validate_adjustment,stage_snapshot

class WaterController(BrewApiController):
 @http.route(['/api/v1/water-sources','/api/v1/water-sources/<int:source_id>'],type='http',auth='public',methods=['GET','POST'],csrf=False)
 def sources(self,source_id=None):
  rid=self._request_id()
  try:
   user,company,session,mode=self._authenticate();service=WaterService(request.env,user,company)
   if request.httprequest.method=='GET':return self._response(service.list(request.httprequest.args.get('factory_id')),request_id=rid)
   self._validate_cookie_csrf(mode);payload=self._json_body()
   data=service.save(payload,source_id)
   self._audit('water_source','update' if source_id else 'create',rid,user,company,{'id':data['id'],'factory_id':data['factory_id']})
   return self._response(data,request_id=rid)
  except ApiError as exc:return self._error_response(exc,rid)
  except (ValueError,TypeError) as exc:return self._error_response(ApiError('VALIDATION_FORMAT',str(exc),422),rid)
  except Exception as exc:return self._error_response(exc,rid)

 @http.route(['/api/v1/water-targets','/api/v1/water-targets/<int:target_id>'],type='http',auth='public',methods=['GET','POST'],csrf=False)
 def targets(self,target_id=None):
  rid=self._request_id()
  try:
   user,company,session,mode=self._authenticate();service=WaterService(request.env,user,company)
   if request.httprequest.method=='GET':return self._response(service.targets(request.httprequest.args.get('product_id'),request.httprequest.args.get('factory_id')),request_id=rid)
   self._validate_cookie_csrf(mode);data=service.save_target(self._json_body(),target_id)
   self._audit('water_target','update' if target_id else 'create',rid,user,company,{'id':data['id'],'product_id':data['product_id'],'factory_id':data['factory_id']})
   return self._response(data,request_id=rid)
  except ApiError as exc:return self._error_response(exc,rid)
  except (ValueError,TypeError) as exc:return self._error_response(ApiError('VALIDATION_FORMAT',str(exc),422),rid)
  except Exception as exc:return self._error_response(exc,rid)
