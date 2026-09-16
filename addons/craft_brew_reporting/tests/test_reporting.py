import base64

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestReporting(TransactionCase):
    def test_purchase_export_generates_csv_attachment(self):
        job = self.env["brew.export.job"].create(
            {
                "company_id": self.env.company.id,
                "requested_by_id": self.env.user.id,
                "export_type": "purchase",
                "filters_json": "{}",
            }
        )
        job.action_process()
        self.assertEqual(job.state, "done")
        self.assertTrue(job.attachment_id)
        content = base64.b64decode(job.attachment_id.datas).decode("utf-8-sig")
        self.assertTrue(content.startswith("order_no,supplier,order_date"))

    def test_export_type_is_restricted_to_confirmed_scope(self):
        with self.assertRaises(ValidationError):
            self.env["brew.export.job"].create(
                {
                    "company_id": self.env.company.id,
                    "requested_by_id": self.env.user.id,
                    "export_type": "payment",
                    "filters_json": "{}",
                }
            )
