from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInventory(TransactionCase):
    def setUp(self):
        super().setUp()
        self.factory = self.env["brew.factory"].create(
            {"name": "库存测试厂", "code": "INV", "company_id": self.env.company.id}
        )
        self.warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        self.warehouse.write({"brew_factory_id": self.factory.id})
        self.factory.stock_warehouse_id = self.warehouse
        self.source = self.warehouse.lot_stock_id
        self.destination = self.env["stock.location"].create(
            {
                "name": "生产暂存",
                "location_id": self.warehouse.view_location_id.id,
                "usage": "internal",
                "company_id": self.env.company.id,
                "brew_factory_id": self.factory.id,
            }
        )
        template = self.env["product.template"].create(
            {
                "name": "测试麦芽",
                "company_id": self.env.company.id,
                "detailed_type": "product",
                "tracking": "lot",
                "brew_item_type": "raw_material",
                "brew_material_category": "malt",
            }
        )
        self.product = template.product_variant_id
        self.lot = self.env["stock.lot"].create(
            {
                "name": "MALT-001",
                "product_id": self.product.id,
                "company_id": self.env.company.id,
                "brew_quality_status": "qualified",
                "brew_expiration_date": fields.Date.today(),
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product, self.source, 10, lot_id=self.lot
        )

    def _transfer(self, quantity):
        return self.env["brew.inventory.transfer"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "source_location_id": self.source.id,
                "destination_location_id": self.destination.id,
                "reason": "生产备料",
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "lot_id": self.lot.id,
                            "quantity": quantity,
                            "uom_id": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )

    def test_controlled_transfer_preserves_stock_and_source(self):
        transfer = self._transfer(4)
        transfer.action_confirm()
        self.assertEqual(transfer.state, "done")
        move = transfer.picking_id.move_ids
        self.assertTrue(move.brew_controlled)
        self.assertEqual(move.brew_source_model, transfer._name)
        self.assertEqual(move.brew_source_id, transfer.id)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product, self.destination, lot_id=self.lot
            ),
            4,
        )

    def test_transfer_blocks_negative_stock(self):
        with self.assertRaises(ValidationError):
            self._transfer(11).action_confirm()

    def test_frozen_lot_is_not_available_for_fefo(self):
        self.lot.action_freeze("质量复检")
        self.assertTrue(self.lot.brew_frozen)
        self.lot.action_unfreeze("复检合格")
        self.assertFalse(self.lot.brew_frozen)

    def test_reservation_quantity_is_normalized_to_product_uom(self):
        reservation = self.env["brew.inventory.reservation"].create(
            {
                "company_id": self.env.company.id,
                "factory_id": self.factory.id,
                "product_id": self.product.id,
                "lot_id": self.lot.id,
                "location_id": self.source.id,
                "quantity": 2,
                "uom_id": self.product.uom_id.id,
                "source_model": "brew.production.plan",
                "source_id": 100,
            }
        )
        self.assertEqual(reservation.quantity_base, 2)
        reservation.action_release()
        self.assertEqual(reservation.state, "released")

    def test_inventory_count_requires_reason_and_links_previous_count(self):
        first = self.env["brew.inventory.count"].create(
            {"company_id": self.env.company.id, "location_id": self.source.id}
        )
        first.action_start()
        first_line = first.line_ids.filtered(lambda line: line.lot_id == self.lot)
        with self.assertRaises(ValidationError):
            first_line.write({"counted_qty": 9})
        first_line.write(
            {"counted_qty": 9, "difference_reason": "measurement"}
        )
        first.write({"state": "done"})

        second = self.env["brew.inventory.count"].create(
            {"company_id": self.env.company.id, "location_id": self.source.id}
        )
        second.action_start()
        second_line = second.line_ids.filtered(lambda line: line.lot_id == self.lot)
        self.assertEqual(second_line.previous_count_line_id, first_line)
