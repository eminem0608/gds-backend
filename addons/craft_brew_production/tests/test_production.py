from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProductionExecution(TransactionCase):
    def setUp(self):
        super().setUp()
        self.factory = self.env["brew.factory"].create(
            {"name": "生产测试厂", "code": "PROD", "company_id": self.env.company.id}
        )
        self.equipment = self.env["maintenance.equipment"].create(
            {
                "name": "糖化线",
                "company_id": self.env.company.id,
                "brew_factory_id": self.factory.id,
                "brew_equipment_type": "mash",
            }
        )
        self.tank = self.env["brew.tank"].create(
            {
                "name": "FV-T1",
                "factory_id": self.factory.id,
                "tank_type": "fermentation",
                "capacity_l": 2000,
                "status": "empty",
                "cleaning_status": "clean",
            }
        )
        product = self.env["product.template"].create(
            {
                "name": "测试啤酒",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "finished_product",
            }
        )
        recipe = self.env["brew.recipe"].create(
            {
                "name": "测试配方",
                "code": "PROD-RCP",
                "company_id": self.env.company.id,
                "product_tmpl_id": product.id,
            }
        )
        version = self.env["brew.recipe.version"].create(
            {
                "recipe_id": recipe.id,
                "version": 1,
                "base_volume_l": 1000,
                "state": "active",
            }
        )
        start = fields.Datetime.now()
        self.plan = self.env["brew.production.plan"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "product_tmpl_id": product.id,
                "recipe_version_id": version.id,
                "planned_volume_l": 1000,
                "planned_start": start,
                "planned_end": start + timedelta(hours=8),
                "equipment_id": self.equipment.id,
                "tank_id": self.tank.id,
                "state": "dispatched",
            }
        )

    def test_batch_requires_approved_cooling_record_before_entering_tank(self):
        batch = self.env["brew.production.batch"].create(
            {"production_plan_id": self.plan.id}
        )
        batch.action_start()
        cooling = self.env["brew.production.record"].create(
            {
                "batch_id": batch.id,
                "record_type": "cooling",
                "parameters_json": '{"outlet_temperature_c": 19}',
            }
        )
        cooling.action_submit()
        cooling.action_approve(self.env.user)
        batch.action_enter_tank(950)
        self.assertEqual(batch.state, "in_tank")
        self.assertEqual(self.tank.current_production_batch_id, batch)
        self.assertEqual(self.tank.status, "fermenting")
