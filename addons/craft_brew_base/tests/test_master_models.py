import base64
import json

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMasterModels(TransactionCase):
    def test_raw_material_requires_category(self):
        with self.assertRaises(ValidationError):
            self.env["product.template"].create(
                {
                    "name": "Test Malt",
                    "company_id": self.env.company.id,
                    "detailed_type": "product",
                    "brew_item_type": "raw_material",
                }
            )

    def test_configuration_activation_preserves_versions(self):
        model = self.env["brew.configuration.rule"]
        first = model.create(
            {
                "name": "低库存规则 V1",
                "code": "LOW_STOCK",
                "rule_kind": "alert",
                "company_id": self.env.company.id,
                "version": 1,
                "config_json": json.dumps({"threshold": 10}),
            }
        )
        second = model.create(
            {
                "name": "低库存规则 V2",
                "code": "LOW_STOCK",
                "rule_kind": "alert",
                "company_id": self.env.company.id,
                "version": 2,
                "config_json": json.dumps({"threshold": 20}),
            }
        )
        first.action_activate()
        second.action_activate()
        self.assertEqual(first.state, "inactive")
        self.assertEqual(second.state, "active")

    def test_csv_import_validation_has_no_master_data_side_effect(self):
        before = self.env["brew.factory"].search_count([])
        content = "name,code,address\n测试厂区,T01,测试地址\n".encode("utf-8-sig")
        batch = self.env["brew.import.batch"].create(
            {
                "name": "厂区导入",
                "resource": "factories",
                "company_id": self.env.company.id,
                "file_name": "factories.csv",
                "file_data": base64.b64encode(content),
            }
        )
        report = batch.parse_and_validate(
            required_columns={"name"}, allowed_columns={"name", "code", "address"}
        )
        self.assertEqual(report["valid_rows"], 1)
        self.assertEqual(batch.state, "ready")
        self.assertEqual(self.env["brew.factory"].search_count([]), before)
