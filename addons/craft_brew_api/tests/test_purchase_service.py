import json
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

from ..controllers.base import ApiError
from ..services.purchase import PurchaseService


@tagged("post_install", "-at_install")
class TestPurchaseService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env.user.write(
            {
                "groups_id": [
                    Command.link(
                        self.env.ref("craft_brew_core.group_brew_admin").id
                    )
                ]
            }
        )
        self.factory = self.env["brew.factory"].create(
            {
                "name": "API 采购拆单测试厂",
                "code": "API-PUR",
                "company_id": self.env.company.id,
            }
        )
        self.suppliers = self.env["res.partner"].create(
            [
                {"name": "拆单供应商 A", "supplier_rank": 1},
                {"name": "拆单供应商 B", "supplier_rank": 1},
            ]
        )
        self.products = self.env["product.template"].create(
            [
                {
                    "name": "拆单测试麦芽 A",
                    "company_id": self.env.company.id,
                    "detailed_type": "product",
                    "brew_item_type": "raw_material",
                    "brew_material_category": "malt",
                    "brew_supplier_id": self.suppliers[0].id,
                },
                {
                    "name": "拆单测试麦芽 B",
                    "company_id": self.env.company.id,
                    "detailed_type": "product",
                    "brew_item_type": "raw_material",
                    "brew_material_category": "malt",
                    "brew_supplier_id": self.suppliers[1].id,
                },
            ]
        ).product_variant_id

    def _approved_requisition(self, product, supplier):
        return self.env["brew.purchase.requisition"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "need_date": fields.Date.today(),
                "state": "approved",
                "preferred_supplier_id": supplier.id,
                "line_ids": [
                    Command.create(
                        {
                            "product_id": product.id,
                            "requested_qty": 5,
                            "approved_qty": 5,
                            "uom_id": product.uom_id.id,
                        }
                    )
                ],
            }
        )

    def test_only_director_or_admin_can_initiate_factory_requisition(self):
        def make_user(login, group):
            return self.env["res.users"].create({
                "name": login,
                "login": login,
                "company_id": self.env.company.id,
                "company_ids": [Command.link(self.env.company.id)],
                "groups_id": [Command.set([
                    self.env.ref("base.group_user").id,
                    self.env.ref(group).id,
                ])],
                "brew_factory_ids": [Command.link(self.factory.id)],
            })

        director = make_user("purchase_director_test", "craft_brew_core.group_brew_production_director")
        purchaser = make_user("purchase_purchaser_test", "craft_brew_core.group_brew_purchase")
        admin = make_user("purchase_admin_test", "craft_brew_core.group_brew_admin")
        requisitions = self.env["brew.purchase.requisition"]
        self.assertTrue(requisitions.with_user(director).check_access_rights("create", raise_exception=False))
        self.assertFalse(requisitions.with_user(purchaser).check_access_rights("create", raise_exception=False))
        self.assertTrue(requisitions.with_user(admin).check_access_rights("create", raise_exception=False))
        request = {
            "request_type": "emergency",
            "factory_id": self.factory.id,
            "need_date": fields.Date.today().isoformat(),
            "emergency_reason": "测试紧急缺料",
            "lines": [{
                "product_id": self.products[0].id,
                "requested_qty": 2,
                "uom_id": self.products[0].uom_id.id,
            }],
        }
        director_service = PurchaseService(self.env, director, self.env.company)
        purchaser_service = PurchaseService(self.env, purchaser, self.env.company)
        record, _data = director_service.create_requisition(request)
        self.assertEqual(record.requester_id, director)
        director_service.update_requisition(record.id, {
            "version": director_service._version(record), "reason": "本厂补料",
        })
        director_service.requisition_action(record.id, "submit", {
            "version": director_service._version(record),
        })
        self.assertEqual(record.state, "submitted")

        for action in (
            lambda: purchaser_service.create_requisition(request),
            lambda: purchaser_service.update_requisition(record.id, {
                "version": purchaser_service._version(record), "reason": "不允许",
            }),
            lambda: purchaser_service.aggregate_requisitions({"plan_ids": []}),
            lambda: purchaser_service.requisition_action(record.id, "submit", {
                "version": purchaser_service._version(record),
            }),
        ):
            with self.assertRaises(ApiError) as denied:
                action()
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

        other_factory = self.env["brew.factory"].create({
            "name": "其他酒厂", "code": "API-PUR-OTHER", "company_id": self.env.company.id,
        })
        with self.assertRaises(ApiError) as denied:
            director_service.create_requisition({**request, "factory_id": other_factory.id})
        self.assertEqual(denied.exception.code, "DATA_SCOPE_DENIED")

        admin_service = PurchaseService(self.env, admin, self.env.company)
        admin_record, _data = admin_service.create_requisition({**request, "factory_id": other_factory.id})
        self.assertEqual(admin_record.requester_id, admin)

    def test_partial_shortage_request_keeps_original_gap_and_remaining_amount(self):
        finished = self.env["product.template"].create({
            "name": "部分采购测试酒品", "company_id": self.env.company.id,
            "detailed_type": "product", "brew_item_type": "finished_product",
        })
        equipment = self.env["maintenance.equipment"].create({
            "name": "部分采购测试糖化设备", "company_id": self.env.company.id,
            "brew_factory_id": self.factory.id, "brew_equipment_type": "mash",
        })
        tank = self.env["brew.tank"].create({
            "name": "部分采购测试罐", "factory_id": self.factory.id,
            "capacity_l": 2000, "cleaning_status": "clean",
        })
        recipe = self.env["brew.recipe"].create({
            "name": "部分采购测试配方", "code": "API-PUR-PARTIAL",
            "company_id": self.env.company.id, "product_tmpl_id": finished.id,
            "equipment_id": equipment.id,
        })
        version = self.env["brew.recipe.version"].create({
            "recipe_id": recipe.id, "version": 1, "base_volume_l": 1000,
            "state": "active",
        })
        start = fields.Datetime.now() + timedelta(days=2)
        plan = self.env["brew.production.plan"].create({
            "name": "API-PUR-PARTIAL-PLAN", "company_id": self.env.company.id,
            "factory_id": self.factory.id, "product_tmpl_id": finished.id,
            "recipe_version_id": version.id, "planned_volume_l": 1000,
            "planned_start": start, "planned_end": start + timedelta(days=1),
            "equipment_id": equipment.id, "tank_id": tank.id,
            "mash_owner_id": self.env.user.id,
            "fermentation_owner_id": self.env.user.id,
            "state": "stocking",
            "material_snapshot_json": json.dumps({"materials": [{
                "material_id": self.products[0].id,
                "uom_id": self.products[0].uom_id.id,
                "quantity": 10,
            }]}),
        })
        service = PurchaseService(self.env, self.env.user, self.env.company)
        payload = {
            "need_date": fields.Date.today().isoformat(),
            "items": [{
                "plan_id": plan.id, "product_id": self.products[0].id,
                "requested_qty": 4,
            }],
        }
        first, data = service.aggregate_requisitions(payload)
        self.assertEqual(first.line_ids.requested_qty, 4)
        self.assertEqual(first.line_ids.allocation_ids.shortage_qty, 4)
        self.assertEqual(first.line_ids.allocation_ids.source_shortage_qty, 10)
        self.assertEqual(data[0]["lines"][0]["allocations"][0]["source_shortage_qty"], 10)
        source = service.requisition_source_plans({"plan_no": plan.name})[0]
        self.assertEqual(source["materials"][0]["requestable_qty"], 6)

        with self.assertRaises(ApiError) as invalid:
            service.aggregate_requisitions({
                **payload, "items": [{**payload["items"][0], "requested_qty": 0}],
            })
        self.assertEqual(invalid.exception.code, "VALIDATION_RANGE")
        second, _data = service.aggregate_requisitions({
            **payload, "items": [{**payload["items"][0], "requested_qty": 6}],
        })
        self.assertEqual(second.line_ids.requested_qty, 6)
        self.assertEqual(second.line_ids.allocation_ids.source_shortage_qty, 10)

        plan.write({"material_snapshot_json": json.dumps({"materials": [{
            "material_id": self.products[0].id,
            "uom_id": self.products[0].uom_id.id,
            "quantity": 12,
        }]})})
        extra, _data = service.aggregate_requisitions({
            **payload, "items": [{**payload["items"][0], "requested_qty": 4}],
        })
        self.assertEqual(extra.line_ids.requested_qty, 4)
        self.assertEqual(extra.line_ids.allocation_ids.source_shortage_qty, 12)
        self.assertEqual(service.requisition_source_plans({"plan_no": plan.name})[0]["materials"][0]["requestable_qty"], 0)

    def test_requisition_lines_are_split_into_orders_by_supplier(self):
        requisitions = (
            self._approved_requisition(self.products[0], self.suppliers[0])
            | self._approved_requisition(self.products[1], self.suppliers[1])
        )
        service = PurchaseService(self.env, self.env.user, self.env.company)

        orders, data = service.create_order_from_requisitions(
            {
                "order_date": fields.Date.today().isoformat(),
                "expected_arrival_date": fields.Date.today().isoformat(),
                "lines": [
                    {
                        "requisition_line_id": requisitions[0].line_ids.id,
                        "supplier_id": self.suppliers[0].id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 10,
                    },
                    {
                        "requisition_line_id": requisitions[1].line_ids.id,
                        "supplier_id": self.suppliers[1].id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 20,
                    },
                ],
            }
        )

        self.assertEqual(len(orders), 2)
        self.assertEqual(set(orders.partner_id.ids), set(self.suppliers.ids))
        self.assertEqual(set(orders.user_id.ids), {self.env.user.id})
        self.assertEqual(len(data), 2)
        self.assertEqual(set(requisitions.line_ids.mapped("ordered_qty")), {5.0})

    def test_requisitions_are_newest_first_and_filterable(self):
        first = self._approved_requisition(self.products[0], self.suppliers[0])
        second = self._approved_requisition(self.products[1], self.suppliers[1])
        service = PurchaseService(self.env, self.env.user, self.env.company)

        records, total = service.requisitions(1, 100)
        filtered, filtered_total = service.requisitions(
            1,
            100,
            {
                "keyword": self.products[0].display_name,
                "supplier_name": self.suppliers[0].name,
                "state": "approved",
            },
        )

        first_index = next(
            index for index, item in enumerate(records) if item["id"] == str(first.id)
        )
        second_index = next(
            index for index, item in enumerate(records) if item["id"] == str(second.id)
        )
        self.assertGreater(first_index, second_index)
        self.assertGreaterEqual(total, 2)
        self.assertEqual(filtered_total, 1)
        self.assertEqual(filtered[0]["id"], str(first.id))

    def test_approval_history_includes_actor_roles(self):
        requisition = self._approved_requisition(self.products[0], self.suppliers[0])
        requisition.write({"state": "draft"})
        requisition.action_submit(self.env.user)
        history = PurchaseService(
            self.env, self.env.user, self.env.company
        )._serialize_requisition(requisition)["approval_history"]

        self.assertEqual(history[0]["actor"]["name"], self.env.user.name)
        self.assertIn("role", history[0]["actor"])
        self.assertIn("roles", history[0]["actor"])
        self.assertEqual(history[0]["role_label"], "发起人")

    def test_approval_history_includes_resubmission_comment(self):
        requisition = self._approved_requisition(self.products[0], self.suppliers[0])
        order = self.env["purchase.order"].create(
            {
                "company_id": self.env.company.id,
                "partner_id": self.suppliers[0].id,
                "brew_factory_id": self.factory.id,
                "brew_approval_state": "returned",
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.products[0].id,
                            "product_qty": 1,
                            "product_uom": self.products[0].uom_id.id,
                            "price_unit": 10,
                            "date_planned": fields.Datetime.now(),
                            "brew_requisition_line_ids": [
                                Command.set(requisition.line_ids.ids)
                            ],
                        }
                    )
                ],
            }
        )
        order.action_brew_submit(self.env.user, "已调整采购数量和到货日期")

        history = PurchaseService(
            self.env, self.env.user, self.env.company
        )._serialize_order(order)["approval_history"]

        self.assertEqual(history[0]["comment"], "已调整采购数量和到货日期")

    def test_draft_requisition_update_can_add_and_delete_lines(self):
        requisition = self._approved_requisition(self.products[0], self.suppliers[0])
        requisition.write({"state": "draft", "line_ids": [Command.update(requisition.line_ids.id, {"approved_qty": 0})]})
        service = PurchaseService(self.env, self.env.user, self.env.company)

        updated, data = service.update_requisition(
            requisition.id,
            {
                "version": service._version(requisition),
                "need_date": fields.Date.today().isoformat(),
                "supplier_id": self.suppliers[1].id,
                "lines": [
                    {
                        "product_id": self.products[1].id,
                        "uom_id": self.products[1].uom_id.id,
                        "requested_qty": 3,
                    }
                ],
            },
        )

        self.assertEqual(updated.state, "draft")
        self.assertEqual(updated.preferred_supplier_id, self.suppliers[1])
        self.assertEqual(updated.line_ids.product_id, self.products[1])
        self.assertEqual(updated.line_ids.requested_qty, 3)
        self.assertEqual(len(data["lines"]), 1)

    def test_returned_requisition_can_update_existing_line(self):
        requisition = self._approved_requisition(self.products[0], self.suppliers[0])
        requisition.write({"state": "returned", "line_ids": [Command.update(requisition.line_ids.id, {"approved_qty": 0})]})
        service = PurchaseService(self.env, self.env.user, self.env.company)

        service.update_requisition(
            requisition.id,
            {
                "version": service._version(requisition),
                "lines": [
                    {
                        "id": requisition.line_ids.id,
                        "product_id": self.products[0].id,
                        "uom_id": self.products[0].uom_id.id,
                        "requested_qty": 8,
                    }
                ],
            },
        )

        self.assertEqual(requisition.line_ids.requested_qty, 8)

    def test_order_update_reconciles_ordered_qty_when_replacing_line(self):
        first = self._approved_requisition(self.products[0], self.suppliers[0])
        second = self._approved_requisition(self.products[1], self.suppliers[1])
        service = PurchaseService(self.env, self.env.user, self.env.company)
        orders, _data = service.create_order_from_requisitions(
            {
                "order_date": fields.Date.today().isoformat(),
                "expected_arrival_date": fields.Date.today().isoformat(),
                "lines": [
                    {
                        "requisition_line_id": first.line_ids.id,
                        "supplier_id": self.suppliers[0].id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 10,
                    }
                ],
            }
        )
        order = orders.ensure_one()

        service.update_order(
            order.id,
            {
                "version": service._version(order),
                "lines": [
                    {
                        "requisition_line_id": second.line_ids.id,
                        "quantity": 3,
                        "package_count": 1,
                        "price_unit": 20,
                    }
                ],
            },
        )

        self.assertEqual(first.line_ids.ordered_qty, 0)
        self.assertEqual(second.line_ids.ordered_qty, 3)
        self.assertEqual(order.order_line.product_id, self.products[1])
        self.assertEqual(order.brew_requisition_ids, second)

    def test_order_update_can_append_line_with_serialized_planned_date(self):
        first = self._approved_requisition(self.products[0], self.suppliers[0])
        second = self._approved_requisition(self.products[1], self.suppliers[1])
        service = PurchaseService(self.env, self.env.user, self.env.company)
        orders, _data = service.create_order_from_requisitions(
            {
                "lines": [
                    {
                        "requisition_line_id": first.line_ids.id,
                        "supplier_id": self.suppliers[0].id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 10,
                    }
                ],
            }
        )
        order = orders.ensure_one()
        existing = order.order_line

        service.update_order(
            order.id,
            {
                "version": service._version(order),
                "lines": [
                    {
                        "id": existing.id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 10,
                        "planned_date": existing.date_planned.isoformat(),
                    },
                    {
                        "requisition_line_id": second.line_ids.id,
                        "quantity": 3,
                        "package_count": 1,
                        "price_unit": 20,
                        "planned_date": existing.date_planned.isoformat(),
                    },
                ],
            },
        )

        self.assertEqual(len(order.order_line), 2)
        self.assertEqual(first.line_ids.ordered_qty, 5)
        self.assertEqual(second.line_ids.ordered_qty, 3)

    def test_approved_order_cannot_be_edited(self):
        requisition = self._approved_requisition(self.products[0], self.suppliers[0])
        service = PurchaseService(self.env, self.env.user, self.env.company)
        orders, _data = service.create_order_from_requisitions(
            {
                "lines": [
                    {
                        "requisition_line_id": requisition.line_ids.id,
                        "supplier_id": self.suppliers[0].id,
                        "quantity": 5,
                        "package_count": 1,
                        "price_unit": 10,
                    }
                ],
            }
        )
        order = orders.ensure_one()
        order.brew_approval_state = "approved"

        with self.assertRaises(ApiError):
            service.update_order(
                order.id,
                {"version": service._version(order), "supplier_id": self.suppliers[1].id},
            )
