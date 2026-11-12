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

    def brew_scope_factory(self, company=None):
        """A factory ancestor takes precedence over all role-based privileges."""
        self.ensure_one()
        company = company or self.env.company
        organization = self.brew_organization_id
        while organization:
            if organization.company_id != company:
                return self.env['brew.factory']
            if organization.organization_type == 'factory':
                return organization.factory_id
            organization = organization.parent_id
        return self.env['brew.factory']

    def brew_has_headquarters_scope(self, company=None):
        self.ensure_one()
        company = company or self.env.company
        if company not in self.company_ids:
            return False
        organization = self.brew_organization_id
        if organization:
            if organization.company_id != company or self.brew_scope_factory(company):
                return False
            # An unlinked factory organization must never fall back to headquarters.
            ancestors = self._organization_ancestors()
            return bool(ancestors) and all(node.active for node in ancestors) and not any(node.organization_type == 'factory' for node in ancestors) and any(node.organization_type == 'company' for node in ancestors)
        # Preserve existing headquarters accounts until their organization is assigned.
        return bool(set(self.brew_role_codes()) & {'tenant_admin', 'general_manager', 'production_director', 'headquarters_planner'})

    def brew_allowed_factories(self, company=None, include_archived=False):
        self.ensure_one()
        company = company or self.env.company
        model = self.env['brew.factory'].sudo().with_context(active_test=False)
        if company not in self.company_ids:
            return model.browse()
        if self.brew_organization_id and not all(node.active for node in self._organization_ancestors()):
            return model.browse()
        if self.brew_has_headquarters_scope(company):
            domain = [('company_id', '=', company.id)]
        elif self.brew_organization_id:
            domain = [('company_id', '=', company.id), ('id', 'in', self.brew_scope_factory(company).ids)]
        else:
            factories = self.brew_factory_ids.filtered(lambda f: f.company_id == company)
            preferred = factories & self.brew_default_factory_id
            domain = [('company_id', '=', company.id), ('id', 'in', (preferred or factories[:1]).ids)]
        if not include_archived:
            domain.append(('active', '=', True))
        return model.search(domain, order='name,id')

    def brew_context_payload(self):
        payload = super().brew_context_payload()
        self.ensure_one()
        current_company = self.env.company
        factories = self.brew_allowed_factories(current_company)
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
                "data_scope": {"kind": "headquarters" if self.brew_has_headquarters_scope(current_company) else "factory",
                               "factory_ids": [str(factory.id) for factory in factories]},
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
