from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTraceCodeScope(TransactionCase):
    def test_v1_rejects_bottle_and_can_code_generation(self):
        model = self.env["brew.trace.code"]
        empty_order = self.env["brew.packaging.order"]
        with self.assertRaises(ValidationError):
            model.create_for_order(empty_order, "bottle", 1)
        with self.assertRaises(ValidationError):
            model.create_for_order(empty_order, "can", 1)
