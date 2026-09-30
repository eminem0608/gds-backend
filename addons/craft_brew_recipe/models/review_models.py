from odoo import fields, models


class BrewRecipeReviewEvent(models.Model):
    _name = "brew.recipe.review.event"
    _description = "Craft Brew Recipe Review Event"
    _order = "event_at, id"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    recipe_id = fields.Many2one("brew.recipe", required=True, index=True, ondelete="cascade")
    version_id = fields.Many2one("brew.recipe.version", required=True, index=True, ondelete="cascade")
    review_round = fields.Integer(required=True, default=1, index=True)
    action = fields.Selection(
        [
            ("submitted", "提交审核"),
            ("approved", "审核通过"),
            ("returned", "退回修改"),
            ("activated", "启用版本"),
            ("deactivated", "停用版本"),
        ],
        required=True,
        index=True,
    )
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="restrict")
    comment = fields.Text()
    event_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)


