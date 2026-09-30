from odoo import api, fields, models
from odoo.exceptions import ValidationError


class BrewOrganization(models.Model):
    _inherit = "brew.organization"

    factory_id = fields.Many2one(
        "brew.factory",
        string="对应厂区",
        index=True,
        ondelete="restrict",
        domain="[('company_id', '=', company_id)]",
    )

    _sql_constraints = [
        ("brew_organization_factory_unique", "unique(factory_id)", "一个厂区只能对应一个组织节点。"),
    ]

    @api.constrains("company_id", "organization_type", "factory_id")
    def _check_factory_link(self):
        for organization in self:
            if not organization.factory_id:
                continue
            if organization.organization_type != "factory":
                raise ValidationError("只有厂区类型的组织节点可以关联厂区数据。")
            if organization.factory_id.company_id != organization.company_id:
                raise ValidationError("组织节点和厂区数据必须属于同一企业。")
