from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRecipe(TransactionCase):
    def setUp(self):
        super().setUp()
        self.finished = self.env["product.template"].create(
            {
                "name": "测试成品",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "finished_product",
            }
        )
        self.material = self.env["product.template"].create(
            {
                "name": "测试麦芽",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "brew_item_type": "raw_material",
                "brew_material_category": "malt",
                "standard_price": 8.0,
            }
        ).product_variant_id
        self.recipe = self.env["brew.recipe"].create(
            {
                "name": "测试配方",
                "code": "TEST-RECIPE",
                "company_id": self.env.company.id,
                "product_tmpl_id": self.finished.id,
            }
        )
        self.version = self.env["brew.recipe.version"].create(
            {"recipe_id": self.recipe.id, "version": 1, "base_volume_l": 1000}
        )
        self.env["brew.recipe.material"].create(
            {
                "version_id": self.version.id,
                "product_id": self.material.id,
                "quantity": 200,
                "uom_id": self.material.uom_id.id,
                "stage": "mash",
            }
        )

    def _create_step(self, action_type, sequence):
        return self.env["brew.recipe.process.step"].create(
            {
                "version_id": self.version.id,
                "name": action_type,
                "sequence": sequence,
                "phase": "packaging" if action_type == "packaging_release" else "fermentation",
                "action_type": action_type,
                "mandatory_confirmation": True,
            }
        )

    def test_submit_requires_all_mandatory_confirmation_steps(self):
        self._create_step("enter_tank", 10)
        with self.assertRaises(ValidationError):
            self.version.action_submit()
        self._create_step("transfer", 20)
        self._create_step("packaging_release", 30)
        self.version.action_submit()
        self.assertEqual(self.version.state, "submitted")
        self.assertGreater(self.version.estimated_cost, 0)

    def test_approval_makes_version_available_and_immutable(self):
        for index, action in enumerate(["enter_tank", "transfer", "packaging_release"], start=1):
            self._create_step(action, index * 10)
        self.version.action_submit()
        self.version.action_approve()
        self.assertEqual(self.version.state, "active")
        self.assertEqual(self.recipe.active_version_id, self.version)
        self.assertEqual(self.version.approved_by_id, self.env.user)
        self.assertTrue(self.version.approved_at)
        with self.assertRaises(ValidationError):
            self.version.base_volume_l = 2000
        with self.assertRaises(ValidationError):
            self.version.material_line_ids.quantity = 300

    def test_new_approval_replaces_previous_production_version(self):
        for index, action in enumerate(["enter_tank", "transfer", "packaging_release"], start=1):
            self._create_step(action, index * 10)
        self.version.action_submit()
        self.version.action_approve()

        next_version = self.version.copy_as_draft()
        next_version.action_submit()
        next_version.action_approve()

        self.assertEqual(self.version.state, "inactive")
        self.assertEqual(next_version.state, "active")
        self.assertEqual(self.recipe.active_version_id, next_version)
        self.assertEqual(
            self.env["brew.recipe.review.event"].search_count(
                [("version_id", "=", next_version.id), ("action", "=", "approved")]
            ),
            1,
        )

    def test_calculate_scales_material_and_estimated_cost(self):
        result = self.version.calculate(500)
        self.assertEqual(result["materials"][0]["quantity"], 100)
        self.assertAlmostEqual(result["estimated_cost"], 800.0)

    def test_same_product_and_equipment_can_have_multiple_recipes(self):
        equipment = self.env["maintenance.equipment"].create({
            "name": "测试酿造系统",
            "company_id": self.env.company.id,
            "brew_equipment_type": "mash",
        })
        self.recipe.equipment_id = equipment
        another = self.env["brew.recipe"].create({
            "name": "测试配方 B",
            "code": "TEST-RECIPE-B",
            "company_id": self.env.company.id,
            "product_tmpl_id": self.finished.id,
            "equipment_id": equipment.id,
        })
        self.assertNotEqual(self.recipe.id, another.id)
