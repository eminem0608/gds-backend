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

    def test_approval_can_be_returned_repeatedly_after_resubmission(self):
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
        instances = []
        for cycle in range(1, 5):
            approval = requisition.action_submit(self.env.user)
            reason = f"第{cycle}次退回"
            approval.step_ids[0].action_return(self.env.user, reason)
            requisition.action_return(reason)
            instances.append(approval)

        self.assertTrue(all(item.state == "returned" for item in instances))
        self.assertTrue(
            all(item.step_ids[0].state == "returned" for item in instances)
        )
        self.assertEqual(instances[-1].step_ids[0].comment, "第4次退回")
        self.assertEqual(requisition.return_reason, "第4次退回")

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

    def test_purchase_order_keeps_delivery_and_package_information(self):
        supplier = self.env["res.partner"].create(
            {"name": "Package Test Supplier", "supplier_rank": 1}
        )
        order = self.env["purchase.order"].create(
            {
                "company_id": self.env.company.id,
                "partner_id": supplier.id,
                "brew_factory_id": self.factory.id,
                "brew_expected_arrival_date": "2026-10-20",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_qty": 14,
                            "product_uom": self.product.uom_id.id,
                            "price_unit": 12.5,
                            "brew_package_spec": "25 kg/bag",
                            "brew_package_count": 14,
                            "date_planned": fields.Datetime.now(),
                        },
                    )
                ],
            }
        )

        self.assertEqual(
            fields.Date.to_string(order.brew_expected_arrival_date), "2026-10-20"
        )
        self.assertEqual(order.order_line.brew_package_spec, "25 kg/bag")
        self.assertEqual(order.order_line.brew_package_count, 14)

    def test_purchase_order_approval_can_be_returned_repeatedly(self):
        supplier = self.env["res.partner"].create(
            {"name": "Repeated Return Supplier", "supplier_rank": 1}
        )
        order = self.env["purchase.order"].create(
            {
                "company_id": self.env.company.id,
                "partner_id": supplier.id,
                "brew_factory_id": self.factory.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_qty": 1,
                            "product_uom": self.product.uom_id.id,
                            "price_unit": 10,
                            "date_planned": fields.Datetime.now(),
                        },
                    )
                ],
            }
        )

        instances = []
        for cycle in range(1, 5):
            note = None if cycle == 1 else f"已按第{cycle - 1}次意见修改"
            approval = order.action_brew_submit(self.env.user, note)
            reason = f"第{cycle}次退回"
            approval.step_ids[0].action_return(self.env.user, reason)
            order.write(
                {
                    "brew_approval_state": "returned",
                    "brew_approval_return_reason": reason,
                }
            )
            instances.append(approval)

        self.assertTrue(all(item.state == "returned" for item in instances))
        self.assertEqual(instances[-1].step_ids[0].comment, "第4次退回")
        self.assertEqual(instances[-1].submission_comment, "已按第3次意见修改")
        self.assertEqual(order.brew_approval_return_reason, "第4次退回")

    def test_returned_purchase_order_requires_resubmission_comment(self):
        supplier = self.env["res.partner"].create(
            {"name": "Resubmission Supplier", "supplier_rank": 1}
        )
        order = self.env["purchase.order"].create(
            {
                "company_id": self.env.company.id,
                "partner_id": supplier.id,
                "brew_factory_id": self.factory.id,
                "brew_approval_state": "returned",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_qty": 1,
                            "product_uom": self.product.uom_id.id,
                            "price_unit": 10,
                            "date_planned": fields.Datetime.now(),
                        },
                    )
                ],
            }
        )

        with self.assertRaises(ValidationError):
            order.action_brew_submit(self.env.user)

    def test_quality_inspection_can_be_returned_repeatedly_then_approved(self):
        supplier = self.env["res.partner"].create(
            {"name": "Quality Approval Supplier", "supplier_rank": 1}
        )
        order = self.env["purchase.order"].create(
            {
                "company_id": self.env.company.id,
                "partner_id": supplier.id,
                "brew_factory_id": self.factory.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_qty": 10,
                            "product_uom": self.product.uom_id.id,
                            "price_unit": 8,
                            "date_planned": fields.Datetime.now(),
                        },
                    )
                ],
            }
        )
        qc_location = self.env["stock.location"].create(
            {"name": "Quality Approval QC", "usage": "internal", "company_id": self.env.company.id}
        )
        stock_location = self.env["stock.location"].create(
            {"name": "Quality Approval Stock", "usage": "internal", "company_id": self.env.company.id}
        )
        receipt = self.env["brew.receipt"].create(
            {
                "purchase_order_id": order.id,
                "qc_location_id": qc_location.id,
                "destination_location_id": stock_location.id,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "purchase_order_line_id": order.order_line.id,
                            "received_qty": 10,
                            "lot_name": "QUALITY-APPROVAL-LOT",
                        },
                    )
                ],
            }
        )
        lot = self.env["stock.lot"].create(
            {
                "name": "QUALITY-APPROVAL-LOT",
                "product_id": self.product.id,
                "company_id": self.env.company.id,
            }
        )
        receipt.line_ids.lot_id = lot
        inspection = self.env["brew.quality.inspection"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "receipt_id": receipt.id,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "receipt_line_id": receipt.line_ids.id,
                            "product_id": self.product.id,
                            "lot_id": lot.id,
                            "inspected_qty": 10,
                            "qualified_qty": 10,
                            "rejected_qty": 0,
                            "uom_id": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )

        returned_instances = []
        for cycle in range(1, 4):
            approval = inspection.action_submit(self.env.user, f"第{cycle}次提交")
            reason = f"第{cycle}次退回质检"
            approval.step_ids[0].action_return(self.env.user, reason)
            inspection.action_return(reason)
            returned_instances.append(approval)

        final_approval = inspection.action_submit(self.env.user, "已完成全部修改")
        for step in final_approval.step_ids.sorted("sequence"):
            step.action_approve(self.env.user, "同意质检结论")
        inspection.action_approve()

        self.assertTrue(all(item.state == "returned" for item in returned_instances))
        self.assertEqual(inspection.state, "completed")
        self.assertEqual(receipt.state, "inspected")
