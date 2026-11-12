"""Rename only Odoo's default company / headquarters labels; preview by default."""
import sys
import odoo
from odoo import api, SUPERUSER_ID
odoo.tools.config.parse_config(['-c','/etc/odoo/odoo.conf','-d','craft_brew_dev','--no-http'])
with odoo.registry('craft_brew_dev').cursor() as cr:
 env=api.Environment(cr,SUPERUSER_ID,{})
 companies=env['res.company'].search([('name','in',['My Company','MyCompany'])])
 organizations=env['brew.organization'].with_context(active_test=False).search([('organization_type','=','company'),('name','in',['My Company','MyCompany'])])
 print('Default companies:',companies.ids,'headquarters organizations:',organizations.ids)
 if '--apply' in sys.argv:
  companies.write({'name':'总部'})
  organizations.write({'name':'总部'})
  cr.commit()
  print('Updated default labels to 总部')
 else:cr.rollback()
