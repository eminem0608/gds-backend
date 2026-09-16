from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTenantScope(TransactionCase):
    def test_default_factory_must_be_authorized(self):
        company = self.env.company
        first = self.env["brew.factory"].create(
            {"name": "一厂", "code": "F01", "company_id": company.id}
        )
        second = self.env["brew.factory"].create(
            {"name": "二厂", "code": "F02", "company_id": company.id}
        )
        user = self.env["res.users"].create(
            {
                "name": "Factory User",
                "login": "factory-test-user",
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
                "brew_factory_ids": [(6, 0, [first.id])],
            }
        )
        with self.assertRaises(ValidationError):
            user.brew_default_factory_id = second

    def test_factory_code_is_unique_within_company(self):
        values = {"name": "一厂", "code": "UNIQUE", "company_id": self.env.company.id}
        self.env["brew.factory"].create(values)
        with self.assertRaises(Exception):
            self.env["brew.factory"].create({**values, "name": "重复编码"})
