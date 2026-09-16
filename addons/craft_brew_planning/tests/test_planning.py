from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPlanning(TransactionCase):
    def setUp(self):
        super().setUp()
        self.factory = self.env["brew.factory"].create({"name": "计划厂", "code": "PLAN", "company_id": self.env.company.id})
        self.equipment = self.env["maintenance.equipment"].create({"name": "糖化线", "company_id": self.env.company.id, "brew_factory_id": self.factory.id, "brew_equipment_type": "mash"})
        self.tank = self.env["brew.tank"].create({"name": "FV-P1", "factory_id": self.factory.id, "tank_type": "fermentation", "capacity_l": 2000})
        product = self.env["product.template"].create({"name": "计划成品", "company_id": self.env.company.id, "detailed_type": "product", "brew_item_type": "finished_product"})
        recipe = self.env["brew.recipe"].create({"name": "计划配方", "code": "PLAN-RCP", "company_id": self.env.company.id, "product_tmpl_id": product.id})
        self.version = self.env["brew.recipe.version"].create({"recipe_id": recipe.id, "version": 1, "base_volume_l": 1000, "state": "active"})
        self.start = fields.Datetime.now() + timedelta(days=1)
        self.end = self.start + timedelta(hours=8)

    def _plan(self, suffix="1"):
        return self.env["brew.production.plan"].create({"name": f"PLN-{suffix}", "company_id": self.env.company.id, "factory_id": self.factory.id, "product_tmpl_id": self.version.recipe_id.product_tmpl_id.id, "recipe_version_id": self.version.id, "planned_volume_l": 1000, "planned_start": self.start, "planned_end": self.end, "equipment_id": self.equipment.id, "tank_id": self.tank.id})

    def test_resource_check_detects_overlapping_plan(self):
        self._plan("1")
        second = self._plan("2")
        result = second.check_resources()
        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"][0]["type"], "production_plan")

    def test_resource_check_detects_unavailability(self):
        self.env["brew.equipment.unavailability"].create({"name": "维护", "equipment_id": self.equipment.id, "start_at": self.start, "end_at": self.end})
        result = self._plan("3").check_resources()
        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"][0]["type"], "equipment_unavailability")
