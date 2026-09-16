from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDemand(TransactionCase):
    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create(
            {
                "name": "测试客户",
                "company_id": self.env.company.id,
                "customer_rank": 1,
            }
        )
        self.product = self.env["product.template"].create(
            {
                "name": "测试啤酒",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "finished_product",
            }
        )
        self.demand = self.env["brew.product.demand"].create(
            {
                "company_id": self.env.company.id,
                "customer_id": self.customer.id,
                "product_tmpl_id": self.product.id,
                "package_qty": 10,
                "requested_volume_l": 200,
                "requested_delivery_date": fields.Date.today() + timedelta(days=7),
            }
        )

    def test_submit_and_confirm(self):
        self.demand.action_submit()
        self.assertEqual(self.demand.state, "submitted")
        self.demand.action_confirm()
        self.assertEqual(self.demand.state, "confirmed")
        self.assertTrue(self.demand.confirmed_at)

    def test_return_requires_reason_and_allows_edit(self):
        self.demand.action_submit()
        with self.assertRaises(ValidationError):
            self.demand.action_return("")
        self.demand.action_return("调整交期")
        self.demand.requested_volume_l = 250
        self.assertEqual(self.demand.state, "returned")
        self.assertEqual(self.demand.requested_volume_l, 250)

    def test_submitted_demand_cannot_be_overwritten(self):
        self.demand.action_submit()
        with self.assertRaises(ValidationError):
            self.demand.requested_volume_l = 250
