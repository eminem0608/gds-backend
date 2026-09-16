from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFermentationFlow(TransactionCase):
    def setUp(self):
        super().setUp()
        self.factory = self.env["brew.factory"].create(
            {"name": "Fermentation Test Factory", "code": "FERM", "company_id": self.env.company.id}
        )
        self.equipment = self.env["maintenance.equipment"].create(
            {
                "name": "Test Brewhouse",
                "company_id": self.env.company.id,
                "brew_factory_id": self.factory.id,
                "brew_equipment_type": "mash",
            }
        )
        self.source_tank = self._create_tank("FV-01")
        self.target_tank = self._create_tank("FV-02")
        product = self.env["product.template"].create(
            {
                "name": "Fermentation Test Beer",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "finished_product",
            }
        )
        recipe = self.env["brew.recipe"].create(
            {
                "name": "Fermentation Test Recipe",
                "code": "FERM-RCP",
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
        plan = self.env["brew.production.plan"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "product_tmpl_id": product.id,
                "recipe_version_id": version.id,
                "planned_volume_l": 1000,
                "planned_start": start,
                "planned_end": start + timedelta(hours=8),
                "equipment_id": self.equipment.id,
                "tank_id": self.source_tank.id,
                "state": "dispatched",
            }
        )
        batch = self.env["brew.production.batch"].create({"production_plan_id": plan.id})
        batch.action_start()
        cooling = self.env["brew.production.record"].create(
            {
                "batch_id": batch.id,
                "record_type": "cooling",
                "parameters_json": '{"outlet_temperature_c": 18}',
            }
        )
        cooling.action_submit()
        cooling.action_approve(self.env.user)
        batch.action_enter_tank(950)
        self.session = batch.fermentation_session_id

    def _create_tank(self, name):
        return self.env["brew.tank"].create(
            {
                "name": name,
                "factory_id": self.factory.id,
                "tank_type": "fermentation",
                "capacity_l": 2000,
                "status": "empty",
                "cleaning_status": "clean",
            }
        )

    def test_approved_reading_updates_tank_snapshot(self):
        reading = self.env["brew.fermentation.reading"].create(
            {
                "session_id": self.session.id,
                "temperature_c": 18.5,
                "pressure_bar": 1.2,
                "gravity_plato": 11.4,
                "ph": 4.3,
                "volume_l": 940,
            }
        )
        reading.action_submit()
        reading.action_approve(self.env.user)

        self.assertEqual(reading.state, "approved")
        self.assertEqual(self.source_tank.current_temperature, 18.5)
        self.assertEqual(self.source_tank.current_pressure, 1.2)
        self.assertEqual(self.source_tank.current_gravity, 11.4)
        self.assertEqual(self.session.current_volume_l, 940)

    def test_transfer_requires_explicit_confirmation(self):
        with self.assertRaises(ValidationError):
            self.session.transfer(self.env.user, self.target_tank, 900, False)

        self.session.transfer(self.env.user, self.target_tank, 900, True)

        self.assertEqual(self.session.tank_id, self.target_tank)
        self.assertEqual(self.session.state, "transferred")
        self.assertEqual(self.source_tank.status, "empty")
        self.assertEqual(self.source_tank.cleaning_status, "dirty")
        self.assertEqual(self.target_tank.status, "conditioning")
        self.assertEqual(self.target_tank.current_volume_l, 900)
