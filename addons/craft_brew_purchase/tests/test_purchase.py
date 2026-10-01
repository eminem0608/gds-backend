from odoo import fields
from odoo.exceptions import ValidationError
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

    def test_pending_requisition_submit_is_idempotent(self):
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
                            "requested_qty": 1,
                            "uom_id": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )
        approval = requisition.action_submit(self.env.user)

        self.assertEqual(requisition.action_submit(self.env.user), approval)
        self.assertEqual(approval.state, "pending")
        self.assertEqual(
            self.env["brew.approval.instance"].search_count(
                [
                    ("target_model", "=", requisition._name),
                    ("target_id", "=", requisition.id),
                    ("state", "=", "pending"),
                ]
            ),
            1,
        )
    def test_returned_requisition_with_pending_approval_can_be_resubmitted(self):
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
                            "requested_qty": 1,
                            "uom_id": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )
        stale_approval = requisition.action_submit(self.env.user)
        requisition.write({"state": "returned", "return_reason": "补充交付日期"})
        new_approval = requisition.action_submit(self.env.user)

        self.assertEqual(stale_approval.state, "returned")
        self.assertTrue(all(step.state == "returned" for step in stale_approval.step_ids))
        self.assertEqual(new_approval.state, "pending")
        self.assertEqual(requisition.state, "submitted")
        self.assertEqual(requisition.return_reason, "补充交付日期")
    def test_emergency_requisition_requires_reason(self):
        values = {
            "company_id": self.env.company.id,
            "factory_id": self.factory.id,
            "need_date": fields.Date.today(),
            "request_type": "emergency",
            "line_ids": [
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "requested_qty": 1,
                        "uom_id": self.product.uom_id.id,
                    },
                )
            ],
        }
        with self.assertRaises(ValidationError):
            self.env["brew.purchase.requisition"].create(values)

        values["emergency_reason"] = "设备故障导致临时增补"
        requisition = self.env["brew.purchase.requisition"].create(values)
        self.assertEqual(requisition.request_type, "emergency")
