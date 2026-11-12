from odoo import api, fields, models
from odoo.exceptions import ValidationError


class BrewProductDemand(models.Model):
    _name = "brew.product.demand"
    _description = "Craft Beer Product Demand"
    _order = "requested_delivery_date, id"
    _check_company_auto = True

    deleted_at = fields.Datetime(readonly=True, copy=False, index=True)
    deleted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    closed_at = fields.Datetime(readonly=True, copy=False)
    closed_by_id = fields.Many2one("res.users", readonly=True, copy=False)

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    salesperson_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, check_company=True, index=True
    )
    customer_id = fields.Many2one(
        "res.partner",
        check_company=True,
        domain="[('customer_rank', '>', 0)]",
        index=True,
    )
    product_tmpl_id = fields.Many2one(
        "product.template",
        required=True,
        check_company=True,
        domain="[('brew_item_type', '=', 'finished_product')]",
        index=True,
    )
    packaging_id = fields.Many2one(
        "product.packaging",
        domain="[('product_id.product_tmpl_id', '=', product_tmpl_id)]",
    )
    packaging_method = fields.Selection(
        [("keg", "桶装"), ("box", "箱装"), ("other", "其他")],
        string="包装方式",
    )
    package_container_type = fields.Selection(
        [("bottle", "瓶"), ("can", "罐")], string="箱内包装类型"
    )
    package_unit_volume_l = fields.Float("单桶或单瓶容量(L)")
    package_units_per_box = fields.Integer("每箱数量")
    packaging_description = fields.Char("其他包装说明")
    package_qty = fields.Float(default=0.0)
    requested_volume_l = fields.Float(required=True)
    requested_delivery_date = fields.Date(required=True, index=True)
    customer_reference = fields.Char()
    notes = fields.Text()
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("submitted", "待确认"),
            ("confirmed", "已确认"),
            ("returned", "已退回"),
            ("planned", "已排产"),
            ("cancelled", "已取消"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    submitted_at = fields.Datetime()
    confirmed_by_id = fields.Many2one("res.users", check_company=True)
    confirmed_at = fields.Datetime()
    decision_reason = fields.Text()

    _sql_constraints = [
        ("company_name_unique", "unique(company_id, name)", "需求单号必须唯一。"),
        ("package_qty_non_negative", "check(package_qty >= 0)", "包装数量不能小于零。"),
        ("requested_volume_positive", "check(requested_volume_l > 0)", "需求酒液体积必须大于零。"),
    ]

    _editable_fields = {
        "factory_id",
        "customer_id",
        "product_tmpl_id",
        "packaging_id",
        "packaging_method",
        "package_container_type",
        "package_unit_volume_l",
        "package_units_per_box",
        "packaging_description",
        "package_qty",
        "requested_volume_l",
        "requested_delivery_date",
        "customer_reference",
        "notes",
    }

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.product.demand") or "New"
        return super().create(values_list)

    def write(self, values):
        if not self.env.context.get("brew_state_transition") and self._editable_fields.intersection(values):
            invalid = self.filtered(lambda item: item.state not in {"draft", "returned"})
            if invalid:
                raise ValidationError("只有草稿或已退回需求可以修改。")
        return super().write(values)

    @api.constrains("packaging_id", "product_tmpl_id")
    def _check_packaging_product(self):
        for demand in self:
            if demand.packaging_id and demand.packaging_id.product_id.product_tmpl_id != demand.product_tmpl_id:
                raise ValidationError("包装规格必须属于所选产品。")

    def action_submit(self):
        self.ensure_one()
        if self.state not in {"draft", "returned"}:
            raise ValidationError("只有草稿或已退回需求可以提交。")
        if self.requested_delivery_date < fields.Date.today():
            raise ValidationError("要求交付日期不能早于今天。")
        self.with_context(brew_state_transition=True).write(
            {"state": "submitted", "submitted_at": fields.Datetime.now(), "decision_reason": False}
        )
        return True

    def action_confirm(self, user=None):
        self.ensure_one()
        if self.state != "submitted":
            raise ValidationError("只有待确认需求可以确认。")
        self.with_context(brew_state_transition=True).write(
            {
                "state": "confirmed",
                "confirmed_by_id": (user or self.env.user).id,
                "confirmed_at": fields.Datetime.now(),
                "decision_reason": False,
            }
        )
        return True

    def action_return(self, reason):
        self.ensure_one()
        if self.state != "submitted" or not str(reason or "").strip():
            raise ValidationError("只有待确认需求可以退回，且必须填写原因。")
        self.with_context(brew_state_transition=True).write(
            {"state": "returned", "decision_reason": reason}
        )
        return True

    def action_cancel(self, reason):
        self.ensure_one()
        if self.state not in {"draft", "returned", "submitted", "confirmed"}:
            raise ValidationError("当前状态不能取消。")
        if not str(reason or "").strip():
            raise ValidationError("取消需求必须填写原因。")
        self.with_context(brew_state_transition=True).write(
            {"state": "cancelled", "decision_reason": reason}
        )
        return True

    def action_mark_planned(self):
        self.ensure_one()
        if self.state != "confirmed":
            raise ValidationError("只有已确认需求可以标记为已排产。")
        self.with_context(brew_state_transition=True).write({"state": "planned"})
        return True
