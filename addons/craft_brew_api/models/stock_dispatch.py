from odoo import api, models
from ..services.planning import PlanningService


class ProductionPlan(models.Model):
    _inherit = "brew.production.plan"

    @api.model
    def _auto_dispatch_for_factory(self, factory):
        # This private entry point is triggered by posted inventory or the cron.
        # Use the configured administrator; a warehouse operator is not granted
        # general permission to edit or dispatch arbitrary production plans.
        actor = self.env.ref("base.user_admin")
        service_env = self.env(user=actor.id, su=True, context=dict(self.env.context, allowed_company_ids=[factory.company_id.id]))
        return PlanningService(service_env, actor, factory.company_id)._auto_dispatch_stocked(factory.id)

    @api.model
    def _cron_auto_dispatch_stocked(self):
        factories = self.sudo().search([("state", "=", "stocking")]).mapped("factory_id")
        for factory in factories:
            self._auto_dispatch_for_factory(factory)


class Receipt(models.Model):
    _inherit = "brew.receipt"

    def action_put_away(self):
        result = super().action_put_away()
        for factory in self.filtered(lambda receipt: receipt.state == "done").mapped("factory_id"):
            self.env["brew.production.plan"]._auto_dispatch_for_factory(factory)
        return result
