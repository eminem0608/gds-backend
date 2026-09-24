from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    _inherit = "res.users"

    brew_supplier_id = fields.Many2one(
        "res.partner", string="供应商主体", domain="[('supplier_rank', '>', 0)]", index=True
    )

    brew_factory_ids = fields.Many2many(
        "brew.factory",
        "brew_factory_user_rel",
        "user_id",
        "factory_id",
        string="授权厂区",
        domain="[('company_id', 'in', company_ids)]",
    )
    brew_default_factory_id = fields.Many2one(
        "brew.factory",
        string="默认厂区",
        domain="[('id', 'in', brew_factory_ids)]",
    )

    @api.constrains("brew_default_factory_id", "brew_factory_ids")
    def _check_brew_default_factory(self):
        for user in self:
            if user.brew_default_factory_id and user.brew_default_factory_id not in user.brew_factory_ids:
                raise ValidationError("默认厂区必须包含在用户的授权厂区中。")

    def brew_context_payload(self):
        payload = super().brew_context_payload()
        self.ensure_one()
        current_company = self.env.company
        factories = self.brew_factory_ids.filtered(
            lambda factory: factory.active and factory.company_id == current_company
        )
        context_factory_id = self.env.context.get("brew_factory_id")
        default_factory = factories.filtered(lambda factory: factory.id == context_factory_id)[:1]
        default_factory = default_factory or self.brew_default_factory_id
        if default_factory not in factories:
            default_factory = factories[:1]
        payload.update(
            {
                "factories": [
                    {
                        "id": str(factory.id),
                        "name": factory.name,
                        "code": factory.code,
                        "company_id": str(factory.company_id.id),
                    }
                    for factory in factories
                ],
                "default_factory_id": str(default_factory.id) if default_factory else None,
            }
        )
        return payload


class BrewApiSession(models.Model):
    _inherit = "brew.api.session"
    _check_company_auto = True

    factory_id = fields.Many2one(
        "brew.factory",
        string="当前厂区",
        domain="[('company_id', '=', company_id)]",
        check_company=True,
    )
