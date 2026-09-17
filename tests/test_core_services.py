import ast
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


security = load_module(
    "security_utils",
    "addons/craft_brew_core/services/security_utils.py",
)
roles = load_module(
    "role_service",
    "addons/craft_brew_core/services/role_service.py",
)


class SecurityUtilsTest(unittest.TestCase):
    def test_generated_token_is_random_and_verifiable(self):
        first = security.generate_token()
        second = security.generate_token()
        self.assertNotEqual(first, second)
        self.assertTrue(security.verify_token(first, security.hash_token(first)))
        self.assertFalse(security.verify_token(first, security.hash_token(second)))

    def test_canonical_hash_ignores_object_key_order(self):
        left = security.canonical_json_hash({"b": 2, "a": {"x": 1}})
        right = security.canonical_json_hash({"a": {"x": 1}, "b": 2})
        self.assertEqual(left, right)


class RoleServiceTest(unittest.TestCase):
    def test_v1_has_exactly_the_confirmed_roles(self):
        self.assertEqual(
            set(roles.ROLE_DEFINITIONS),
            {
                "general_manager",
                "production_director",
                "operator",
                "warehouse_keeper",
                "purchaser",
                "salesperson",
                "finance",
                "tenant_admin",
            },
        )

    def test_permissions_and_menus_are_deduplicated(self):
        selected = ["general_manager", "finance"]
        permissions = roles.permissions_for_roles(selected)
        menus = roles.menus_for_roles(selected)
        self.assertEqual(permissions, sorted(set(permissions)))
        self.assertEqual(len(menus), len({item["code"] for item in menus}))
        self.assertEqual(roles.default_page_for_roles(selected), "dashboard")


class RouteContractTest(unittest.TestCase):
    def test_controller_method_names_are_unique(self):
        owners = {}
        controller_root = ROOT / "addons/craft_brew_api/controllers"
        for path in controller_root.glob("*.py"):
            if path.name in {"__init__.py", "base.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owners.setdefault(item.name, []).append(f"{path.name}:{node.name}")

        duplicates = {name: locations for name, locations in owners.items() if len(locations) > 1}
        self.assertEqual({}, duplicates)

    def test_sprint_one_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        expected = {
            "/api/v1/health/live",
            "/api/v1/health/ready",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/me",
            "/api/v1/auth/logout",
            "/api/v1/auth/sessions",
            "/api/v1/context/switch-company",
            "/api/v1/context/switch-factory",
            "/api/v1/dictionaries",
            "/api/v1/admin/roles",
            "/api/v1/admin/users",
            "/api/v1/admin/audit-logs",
        }
        for route in expected:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)

    def test_sprint_two_resource_contract_is_complete(self):
        service_text = (ROOT / "addons/craft_brew_api/services/master_data.py").read_text(
            encoding="utf-8"
        )
        for resource in {
            "factories",
            "equipment",
            "tanks",
            "warehouses",
            "locations",
            "products",
            "materials",
            "packaging-specs",
            "suppliers",
            "customers",
        }:
            with self.subTest(resource=resource):
                self.assertIn(f'"{resource}"', service_text)

        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        contract_text = controller_text + (ROOT / "addons/craft_brew_api/services/configuration.py").read_text(
            encoding="utf-8"
        )
        for route in {
            "/api/v1/imports",
            "/api/v1/imports/template",
            "/api/v1/admin/number-rules",
            "/api/v1/admin/barcode-rules",
            "/api/v1/admin/alert-rules",
            "/api/v1/admin/approval-rules",
        }:
            with self.subTest(route=route):
                self.assertIn(route.split("/<")[0].rsplit("/", 1)[-1], contract_text)

    def test_sprint_three_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/recipes",
            "/api/v1/recipe-versions",
            "/api/v1/demands",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)

    def test_sprint_four_routes_are_declared(self):
        controller_text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py"))
        for route in {"/api/v1/production-plans", "/api/v1/tasks", "/api/v1/approvals"}:
            with self.subTest(route=route): self.assertIn(route, controller_text)

    def test_sprint_five_inventory_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/inventory/summary",
            "/api/v1/inventory/lots",
            "/api/v1/inventory/movements",
            "/api/v1/inventory/reservations",
            "/api/v1/inventory/transfers",
            "/api/v1/inventory/counts",
            "/api/v1/inventory/scraps",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)
        for action in {"check-inventory", "reserve", "release-reservations"}:
            with self.subTest(action=action):
                self.assertIn(action, controller_text)

    def test_sprint_six_purchase_and_quality_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/purchase-requisitions",
            "/api/v1/purchase-requisitions/aggregate",
            "/api/v1/purchase-orders",
            "/api/v1/purchase-orders/from-requisitions",
            "/api/v1/receipts",
            "/api/v1/quality-inspections",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)

    def test_sprint_seven_material_and_production_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/material-requisitions",
            "/api/v1/production-batches",
            "/api/v1/production-records",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)
        for action in {"recommend-lots", "request-overage", "enter-tank", "correct"}:
            with self.subTest(action=action):
                self.assertIn(action, controller_text)

    def test_sprint_eight_fermentation_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/tanks",
            "/api/v1/fermentation-sessions",
            "/api/v1/fermentation-readings",
            "/api/v1/production-anomalies",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)
        for action in {"add-yeast", "trends", "transfer", "resolve", "verify"}:
            with self.subTest(action=action):
                self.assertIn(action, controller_text)

    def test_sprint_nine_packaging_outbound_and_trace_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/packaging-orders",
            "/api/v1/trace-codes",
            "/api/v1/outbound-orders",
            "/api/v1/trace/<string:code>",
            "/api/v1/trace/lots/<int:lot_id>/forward",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)
        for action in {"generate-codes", "release", "ship"}:
            with self.subTest(action=action):
                self.assertIn(action, controller_text)

    def test_sprint_ten_dashboard_export_and_notification_routes_are_declared(self):
        controller_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "addons/craft_brew_api/controllers").glob("*.py")
        )
        for route in {
            "/api/v1/dashboard/overview",
            "/api/v1/reports/summary",
            "/api/v1/exports",
            "/api/v1/notifications",
            "/api/v1/notification-preferences",
            "/api/v1/admin/operations/metrics",
        }:
            with self.subTest(route=route):
                self.assertIn(route, controller_text)

    def test_financial_export_scope_excludes_payment_and_receipt_processing(self):
        model_text = (ROOT / "addons/craft_brew_reporting/models/reporting_models.py").read_text(encoding="utf-8")
        self.assertIn('EXPORT_TYPES = {"purchase", "inbound", "outbound", "inventory"}', model_text)
        self.assertNotIn('("payment", "Payment")', model_text)
        self.assertNotIn('("collection", "Collection")', model_text)


class FrontendContractTest(unittest.TestCase):
    def test_web_runtime_templates_use_the_vue_compiler_build(self):
        web_root = ROOT.parent / "gds-web"
        app_text = (web_root / "src" / "App.vue").read_text(encoding="utf-8")
        vite_text = (web_root / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn("template: `", app_text)
        self.assertIn('vue/dist/vue.esm-bundler.js', vite_text)

    def test_web_requires_explicit_login_after_every_page_load(self):
        app_text = (ROOT.parent / "gds-web" / "src" / "App.vue").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("restoreSession", app_text)
        self.assertNotIn("brewApi.me()", app_text)
        self.assertIn('v-if="!authContext"', app_text)

    def test_web_operational_pages_use_live_api_contracts(self):
        web_root = ROOT.parent / "gds-web" / "src"
        app_text = (web_root / "App.vue").read_text(encoding="utf-8")
        api_text = (web_root / "api.js").read_text(encoding="utf-8")
        data_text = (web_root / "data.js").read_text(encoding="utf-8")

        self.assertNotIn("demoData", app_text + data_text)
        self.assertNotIn("已保存演示数据", app_text)
        for contract in {
            "listProductionPlans",
            "createProductionPlan",
            "createFermentationReading",
            "listPackagingOrders",
            "ruleAction",
        }:
            with self.subTest(contract=contract):
                self.assertIn(contract, app_text)
                self.assertIn(contract, api_text)
        self.assertIn("approval-rules", app_text)

    def test_planning_read_and_manage_permissions_are_separated(self):
        source = (ROOT / "addons/craft_brew_api/services/planning.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("group_brew_general_manager", source)
        self.assertIn("def _require_manage", source)
        self.assertIn("def _require_approve", source)
        self.assertIn("self._require_manage()", source)


if __name__ == "__main__":
    unittest.main()
