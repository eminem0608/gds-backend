from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPurchaseRequisition(TransactionCase):
    def setUp(self):
        super().setUp()
        self.factory = self.env["brew.factory"].create(
            {"name": "采购测试厂", "code": "PUR", "company_id": self.env.company.id}
        )
        template = self.env["product.template"].create(
            {
                "name": "采购测试麦芽",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "raw_material",
                "brew_material_category": "malt",
            }
        )
        self.product = template.product_variant_id

    def test_requisition_keeps_plan_allocation_and_approval_snapshot(self):
        requisition = self.env["brew.purchase.requisition"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "need_date": fields.Date.today(),
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "requested_qty": 10,
                            "uom_id": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )
        approval = requisition.action_submit(self.env.user)
        self.assertEqual(requisition.state, "submitted")
        self.assertEqual(approval.target_model, requisition._name)
        approval.step_ids[0].action_approve(self.env.user, "同意")
        requisition.action_approve()
        self.assertEqual(requisition.state, "approved")
        self.assertEqual(requisition.line_ids.approved_qty, 10)
