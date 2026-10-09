from odoo import api, fields, models

class ProductTemplate(models.Model):
    _inherit = 'product.template'
    brew_packaging_type = fields.Char('包装材料类型', index=True)
    brew_packaging_details = fields.Json('包装规格与供货信息')
    brew_packaging_state = fields.Selection([('draft','草稿'),('submitted','待确认'),('confirmed','已确认'),('returned','需修改')], string='包装商品状态', index=True)
    brew_packaging_review_note = fields.Text('确认意见')
    brew_packaging_parent_id = fields.Many2one('product.template', string='关联酒厂材料', check_company=True, ondelete='restrict')

class MaterialCategory(models.Model):
    _inherit = 'brew.material.category'
    brew_packaging_type = fields.Char('包装表单类型')

    @api.model
    def _seed_packaging_types(self):
        categories = self.sudo().with_context(active_test=False)
        root = categories.search([('code','=','packaging')],limit=1)
        if not root:
            root = categories.create({'name':'包装材料','code':'packaging','material_group':'packaging'})
        for index,(code,name) in enumerate([('bottle','瓶'),('can','易拉罐'),('keg','桶'),('cap','瓶盖'),('can_lid','罐盖'),('keg_seal','桶盖 / 封签'),('label','标签'),('carton','纸箱 / 礼盒'),('divider','隔板 / 纸托'),('film','缠绕膜'),('strap','打包带')]):
            category = categories.search([('code','=',code),('material_group','=','packaging')],limit=1)
            if not category:
                category = categories.create({'name':name,'code':'pack_'+code,'material_group':'packaging','parent_id':root.id,'sequence':60+index,'brew_packaging_type':code}) if not categories.search_count([('code','=','pack_'+code)]) else categories.search([('code','=','pack_'+code)],limit=1)
            if not category.brew_packaging_type:
                category.write({'brew_packaging_type':code})
        return True
