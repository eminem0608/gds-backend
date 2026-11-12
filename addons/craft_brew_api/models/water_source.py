from odoo import fields, models

class BrewWaterSource(models.Model):
    _name = 'brew.water.source'
    _description = '酒厂水源水质档案'
    _order = 'is_default desc, name, id'
    _check_company_auto = True
    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company',required=True,index=True)
    factory_id = fields.Many2one('brew.factory',required=True,index=True,check_company=True,ondelete='restrict')
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(default=False)
    source_type = fields.Selection([('tap','市政水'),('well','地下水'),('ro','RO 水'),('treated','处理后酿造水'),('other','其他水源')],default='treated',required=True)
    measured_on = fields.Date(required=True)
    report_reference = fields.Char()
    quality_json = fields.Text(required=True,default='{}')
    notes = fields.Text()
    history_json = fields.Text(default='[]',copy=False)

class BrewWaterTarget(models.Model):
    _name = 'brew.water.target'
    _description = '酒品目标水质方案'
    _order = 'is_default desc, name, id'
    _check_company_auto = True
    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company',required=True,index=True)
    product_tmpl_id = fields.Many2one('product.template',required=True,index=True,check_company=True,ondelete='restrict')
    factory_id = fields.Many2one('brew.factory',index=True,check_company=True,ondelete='restrict')
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(default=False)
    ranges_json = fields.Text(required=True,default='{}')
    notes = fields.Text()
    history_json = fields.Text(default='[]',copy=False)
