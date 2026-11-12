"""Physical-count integration checks. Fixtures and stock changes always roll back.

Run inside the Odoo container: python3 /tmp/inventory_count_check.py
"""
import json
import uuid
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import ValidationError


def run(env):
    from odoo.addons.craft_brew_api.controllers.base import ApiError
    from odoo.addons.craft_brew_api.services.inventory import InventoryService
    company = env.company
    admin = env.ref("base.user_admin")
    admin.write({"groups_id": [Command.link(env.ref("craft_brew_core.group_brew_admin").id)]})
    service = InventoryService(env, admin, company)
    code = "IC" + uuid.uuid4().hex[:3].upper()
    factory = env["brew.factory"].create({"name": code, "code": code, "company_id": company.id})
    wh = env["stock.warehouse"].create({"name": code, "code": code, "company_id": company.id, "brew_factory_id": factory.id, "brew_warehouse_type": "raw_material"})
    factory.stock_warehouse_id = wh
    root = wh.lot_stock_id
    root.brew_factory_id = factory
    loc = env["stock.location"].create({"name": "A01", "usage": "internal", "company_id": company.id, "location_id": root.id, "brew_factory_id": factory.id, "brew_location_type": "raw_material"})
    other = env["stock.location"].create({"name": "A02", "usage": "internal", "company_id": company.id, "location_id": root.id, "brew_factory_id": factory.id, "brew_location_type": "raw_material"})
    products = []
    lots = []
    for suffix, unit in [("麦芽", "uom.product_uom_kgm"), ("瓶", "uom.product_uom_unit")]:
        product = env["product.product"].create({"name": code + suffix, "company_id": company.id, "detailed_type": "product", "tracking": "lot", "uom_id": env.ref(unit).id, "uom_po_id": env.ref(unit).id})
        lot = env["stock.lot"].create({"name": code + suffix, "product_id": product.id, "company_id": company.id, "brew_quality_status": "qualified"})
        products.append(product)
        lots.append(lot)
        env["stock.quant"]._update_available_quantity(product, loc, 10, lot_id=lot)

    def stock(product=products[0], location=loc, lot=lots[0]):
        return sum(env["stock.quant"].search([("product_id", "=", product.id), ("location_id", "=", location.id), ("lot_id", "=", lot.id)]).mapped("quantity"))

    def expect_error(fn, kind=(ApiError, ValidationError), text=None):
        with env.cr.savepoint():
            try:
                fn()
            except kind as exc:
                if text:
                    assert text in str(exc), (text, str(exc))
            else:
                raise AssertionError("Invalid operation accepted")

    def create(location=loc, start=True):
        record, data = service.create_count({"location_id": location.id})
        if start:
            record, data = service.count_action(record.id, "start", {"version": data["version"]})
        return record

    def lines(record, quantities=None, reason="measurement"):
        quantities = quantities or {}
        return [{"id": str(line.id), "counted_qty": quantities.get(line.product_id.id, line.theoretical_qty), "difference_reason": reason, "difference_note": "现场核查"} for line in record.line_ids]

    def save(record, rows=None, submit=False):
        return service.update_count_lines(record.id, {"version": service._version(record), "lines": lines(record) if rows is None else rows, "submit_after_save": submit})

    count = create()
    assert count.scope_locked and count.started_at
    assert len(count.line_ids) == 2 and not any(count.line_ids.mapped("is_counted"))
    assert all(line["counted_qty"] is None for line in service._serialize_count(count)["lines"])
    expect_error(lambda: service.count_action(count.id, "submit", {"version": service._version(count)}), text="未盘")
    expect_error(lambda: env["stock.quant"]._update_available_quantity(products[0], loc, 1, lot_id=lots[0]), text="正在盘点")
    quant = env["stock.quant"].search([("product_id", "=", products[0].id), ("location_id", "=", loc.id)], limit=1)
    expect_error(lambda: quant.write({"quantity": 123}), text="正在盘点")
    expect_error(lambda: env["brew.inventory.reservation"].create({"company_id": company.id, "factory_id": factory.id, "product_id": products[0].id, "lot_id": lots[0].id, "location_id": loc.id, "quantity": 1, "uom_id": products[0].uom_id.id, "source_model": count._name, "source_id": count.id}), text="正在盘点")
    overlap = create(root, start=False)
    expect_error(lambda: service.count_action(overlap.id, "start", {"version": service._version(overlap)}), text="重叠")
    env["stock.quant"]._update_available_quantity(products[0], other, 2, lot_id=lots[0])
    assert stock(location=other) == 2, "Other bins must remain operational"
    source = products[0].with_company(company).property_stock_inventory
    for src, dst in [(loc, other), (other, loc)]:
        expect_error(lambda: count._brew_create_done_move(name=code, product=products[0], quantity=1, uom=products[0].uom_id, source_location=src, destination_location=dst, lot=lots[0], source_model=count._name, source_id=count.id, source_reference=count.name), text="正在盘点")

    old = service._version(count)
    save(count, lines(count, {products[0].id: 9}, reason=False))
    assert count.line_ids.filtered(lambda line: line.product_id == products[0]).counted_qty == 9
    expect_error(lambda: service.count_action(count.id, "submit", {"version": service._version(count)}), text="差异原因")
    expect_error(lambda: service.update_count_lines(count.id, {"version": old, "lines": lines(count)}), text="版本")
    first = count.line_ids[0]
    bad_rows = lines(count)
    bad_rows[0]["counted_qty"] = 8
    bad_rows[1]["counted_qty"] = "NaN"
    expect_error(lambda: save(count, bad_rows), text="有效数字")
    assert first.counted_qty == (9 if first.product_id == products[0] else 10), "Partial writes must roll back"
    # Save + submit must be atomic when a difference lacks its reason.
    before = count.line_ids.mapped("counted_qty")
    expect_error(lambda: save(count, lines(count, {products[0].id: 7}, reason=False), True), text="差异原因")
    assert count.line_ids.mapped("counted_qty") == before

    new_product = env["product.product"].create({"name": code + "账外", "company_id": company.id, "detailed_type": "product", "tracking": "none"})
    service.add_count_line(count.id, {"version": service._version(count), "product_id": new_product.id, "location_id": loc.id, "counted_qty": 3, "difference_note": "现场发现，来源已核实"})
    expect_error(lambda: service.add_count_line(count.id, {"version": service._version(count), "product_id": new_product.id, "location_id": loc.id, "counted_qty": 3, "difference_note": "重复"}), text="已在")
    expect_error(lambda: service.add_count_line(count.id, {"version": service._version(count), "product_id": new_product.id, "location_id": other.id, "counted_qty": 3, "difference_note": "越界"}), text="范围")
    rows = lines(count, {products[0].id: 9, products[1].id: 11, new_product.id: 3})
    save(count, rows, True)
    assert count.state == "pending_approval" and count.scope_locked
    assert stock() == 10, "Submission must not adjust inventory"
    metrics = json.loads(count.approval_instance_id.rule_snapshot_json)["metrics"]
    assert metrics["difference_rate"] == 1 and metrics["difference_lines"] == 3
    approval = count.approval_instance_id
    approval.step_ids[0].action_return(admin, "请复盘麦芽")
    assert count._editable_count()
    rows = lines(count, {products[0].id: 8, products[1].id: 11, new_product.id: 3})
    for row in rows:
        if row["id"] == str(count.line_ids.filtered(lambda line: line.product_id == products[0]).id):
            row["recount_note"] = "复称后确认"
    save(count, rows, True)
    assert count.approval_instance_id != approval and approval.state == "returned"
    assert json.loads(count.line_ids.filtered(lambda line: line.product_id == products[0]).recounts_json)[0]["before"] == 9
    while count.approval_instance_id.state == "pending":
        count.approval_instance_id.step_ids.filtered(lambda step: step.state == "pending").sorted("sequence")[0].action_approve(admin)
    # If an adjustment fails, every previously generated movement rolls back.
    real_move = type(count)._brew_create_done_move
    calls = []
    def fail_second(self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise ValidationError("forced stock failure")
        return real_move(self, **kwargs)
    with patch.object(type(count), "_brew_create_done_move", fail_second):
        expect_error(lambda: service.count_action(count.id, "confirm", {"version": service._version(count)}), text="forced stock failure")
    assert count.state == "pending_approval" and count.scope_locked and stock() == 10
    service.count_action(count.id, "confirm", {"version": service._version(count)})
    assert count.state == "done" and not count.scope_locked and stock() == 8
    assert stock(products[1], loc, lots[1]) == 11
    assert len(service._serialize_count(count)["movements"]) == 3
    expect_error(lambda: service.count_action(count.id, "confirm", {"version": service._version(count)}), text="重复")
    assert stock() == 8
    env["stock.quant"]._update_available_quantity(products[0], loc, 1, lot_id=lots[0])
    assert stock() == 9

    no_diff = create()
    save(no_diff, submit=True)
    assert no_diff.state == "done" and not no_diff.scope_locked and not no_diff.approval_instance_id
    cancelled = create()
    save(cancelled, lines(cancelled, {products[0].id: 8}), True)
    service.count_action(cancelled.id, "cancel", {"version": service._version(cancelled), "reason": "现场延期"})
    assert cancelled.state == "cancelled" and not cancelled.scope_locked and cancelled.approval_instance_id.state == "cancelled"
    assert stock() == 9
    empty = env["stock.location"].create({"name": "Empty", "usage": "internal", "company_id": company.id, "location_id": root.id, "brew_factory_id": factory.id})
    empty_count = create(empty)
    assert not empty_count.line_ids
    save(empty_count, [], True)
    assert empty_count.state == "done"
    result, total = service.counts(1, 2, filters={"factory_id": factory.id, "compact": "true"})
    assert total >= 5 and len(result) == 2 and all("lines" not in item for item in result)
    assert service.count_options({"product_id": products[0].id})["lots"]
    print("PASS: blank vs zero, partial save, atomic submit, versions, scope freeze/other-bin operations, movement/quant/reservation protection, overlap, found stock, return/recount/resubmit, mixed-unit metrics, approval/posting separation, failed-post rollback, duplicate posting, no-difference completion, cancellation, empty bins and filtered pagination")


if __name__ == "__main__" and "env" not in globals():
    import odoo
    from odoo import api, SUPERUSER_ID
    odoo.tools.config.parse_config(["-c", "/etc/odoo/odoo.conf", "-d", "craft_brew_dev", "--no-http"])
    with odoo.registry("craft_brew_dev").cursor() as cr:
        try:
            run(api.Environment(cr, SUPERUSER_ID, {}))
        finally:
            cr.rollback()
else:
    try:
        run(env)
    finally:
        env.cr.rollback()
