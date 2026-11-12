"""Batch finished-product identifiers trace raw liquid and all later packaging."""
import json

from ..controllers.base import ApiError


def iso(value):
    return value.isoformat() if value else None


def packaging_data(record):
    product = record.product_id.product_tmpl_id
    return {"id": str(record.id), "code": record.barcode or "", "name": record.name,
            "product": {"id": str(product.id), "name": product.name},
            "type": record.brew_barcode_level, "volume_l": record.brew_volume_l,
            "units": record.qty, "active": record.active}


class ProductTrace:
    def __init__(self, service):
        self.service = service
        self.env, self.company, self.user = service.env, service.company, service.user
        self.factories = service.inventory._allowed_factory_ids()

    def packages(self, code):
        return self.env["product.packaging"].sudo().with_context(active_test=False).search([
            ("product_id.product_tmpl_id.company_id", "=", self.company.id), ("barcode", "=", code)])

    @staticmethod
    def plan_package(plan):
        data = json.loads(plan.packaging_plan_json or "{}")
        return str(data.get("finished_packaging_id") or data.get("packaging_id") or "")

    def _batches(self, package):
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self.factories)]
        orders = self.env["brew.packaging.order"].sudo().search(domain + [
            ("packaging_id", "=", package.id), ("state", "!=", "cancelled")])
        plans = self.env["brew.production.plan"].sudo().search(domain + [
            ("product_tmpl_id", "=", package.product_id.product_tmpl_id.id)])
        plans = plans.filtered(lambda p: self.plan_package(p) == str(package.id))
        return self.env["brew.production.batch"].sudo().search(domain + [
            "|", ("id", "in", orders.mapped("batch_id").ids), ("production_plan_id", "in", plans.ids)],
            order="create_date desc,id desc")

    def search(self, code, page=1, size=20):
        code = str(code or "").strip()
        if not code or len(code) > 256:
            raise ApiError("VALIDATION_REQUIRED", "请输入成品编号、产品编号或追溯码（不超过 256 字）。", 422)
        domain = [("company_id", "=", self.company.id), ("factory_id", "in", self.factories)]
        batch_model = self.env["brew.production.batch"].sudo()
        exact = batch_model.search(domain + [("finished_product_code", "=", code)], limit=1)
        if exact:
            return self._result(self.product_data(exact), exact, page, size)
        batches = batch_model.search(domain + ["|", ("product_code_snapshot", "=", code),
            ("product_tmpl_id.product_variant_ids.default_code", "=", code)], order="create_date desc,id desc")
        if batches:
            return self._result(self.product_data(batches[0], exact=False), batches, page, size)
        packages = self.packages(code)
        exact_batch = None
        if not packages:
            trace = self.env["brew.trace.code"].sudo().search([
                ("company_id", "=", self.company.id), ("factory_id", "in", self.factories),
                ("code", "=", code)], limit=1)
            if trace:
                packages = trace.packaging_order_id.packaging_id
                exact_batch = trace.packaging_order_id.batch_id
        if not packages:
            raise ApiError("RESOURCE_NOT_FOUND", "未找到该编号对应的生产记录，请核对成品编号或产品编号。", 404)
        if len(packages) != 1:
            raise ApiError("VALIDATION_DUPLICATE", "该编号对应多个成品规格，请先核对编号档案。", 422)
        package = packages[0]
        batches = exact_batch if exact_batch else self._batches(package)
        return self._result(packaging_data(package), batches, page, size, package)

    @staticmethod
    def product_data(batch, exact=True):
        product = batch.product_tmpl_id
        return {"id": str(product.id), "code": batch.finished_product_code if exact else batch.product_code_snapshot,
                "name": "", "product": {"id": str(product.id), "name": product.name},
                "kind": "batch" if exact else "product"}

    def _result(self, product, batches, page, size, package=None):
        offset = (page - 1) * size
        return {"product": product, "packaging_id": str(package.id) if package else None,
                "total": len(batches), "page": page,
                "page_size": size, "has_more": page * size < len(batches),
                "batches": [self._summary(batch, package) for batch in batches[offset:offset + size]]}

    def _orders(self, batch, package=None):
        domain = [
            ("company_id", "=", self.company.id), ("factory_id", "in", self.factories),
            ("batch_id", "=", batch.id), ("state", "!=", "cancelled")]
        if package:
            domain.append(("packaging_id", "=", package.id))
        return self.env["brew.packaging.order"].sudo().search(domain)

    def _summary(self, batch, package=None):
        plan = batch.production_plan_id
        data = json.loads(plan.packaging_plan_json or "{}") if not package or self.plan_package(plan) == str(package.id) else {}
        completion = json.loads(plan.brewing_completion_json or "{}")
        orders = self._orders(batch, package)
        execution = data.get("execution") or {}
        last_output = orders.filtered(lambda o: o.submitted_at)[:1]
        packaged = execution.get("completed_at") or iso(last_output.submitted_at)
        return {"id": str(batch.id), "name": batch.name, "plan_name": plan.name,
                "finished_product_code": batch.finished_product_code, "product_code": batch.product_code_snapshot,
                "factory": {"id": str(batch.factory_id.id), "name": batch.factory_id.name},
                "state": "packaged" if packaged else "brewed" if batch.state == "completed" or plan.state == "done" else batch.state,
                "started_at": iso(batch.started_at), "brewed_at": completion.get("confirmed_at") or completion.get("completed_at") or iso(batch.completed_at),
                "packaged_at": packaged, "packaged_by": execution.get("completed_by") or last_output.task_id.assignee_id.name or "",
                "packaging_owner": plan.packaging_owner_id.name if data else "",
                "finished_lots": orders.mapped("finished_lot_id.name")}

    def detail(self, batch_id, package_id=0):
        package = self.env["product.packaging"].sudo().with_context(active_test=False).search([
            ("id", "=", package_id), ("product_id.product_tmpl_id.company_id", "=", self.company.id)], limit=1)
        batch = self.env["brew.production.batch"].sudo().search([
            ("id", "=", batch_id), ("company_id", "=", self.company.id), ("factory_id", "in", self.factories)], limit=1)
        if not batch or (package_id and not package):
            raise ApiError("RESOURCE_NOT_FOUND", "该产品批次不存在或不在当前账号范围内。", 404)
        orders = self._orders(batch, package)
        plan = batch.production_plan_id
        linked = not package_id or self.plan_package(plan) == str(package.id)
        if not linked and not orders:
            raise ApiError("RESOURCE_NOT_FOUND", "该生产批次与所选成品规格没有关联。", 404)
        tasks = self.env["brew.task"].sudo().search([
            ("company_id", "=", self.company.id), ("target_model", "=", plan._name),
            ("target_id", "=", plan.id), ("state", "!=", "cancelled")], order="id")
        mash = tasks.filtered(lambda t: t.task_type == "mashing")[:1]
        fermentation = tasks.filtered(lambda t: t.task_type == "fermentation")[:1]
        packing = tasks.filtered(lambda t: t.task_type == "packaging")[-1:] if linked else tasks.browse()
        from .workflow import WorkflowService
        workflow = WorkflowService(self.env, self.user, self.company)
        nodes = mash.mash_execution_checkpoints() if mash else []
        ferm = workflow.fermentation_execution(fermentation) if fermentation else {}
        data = json.loads(plan.packaging_plan_json or "{}") if linked else {}
        execution = data.get("execution") or {}
        completion = json.loads(plan.brewing_completion_json or "{}")
        events = []

        def event(key, title, at, by=None, detail=None):
            events.append({"key": key, "title": title, "at": at or None,
                           "by": by or None, "detail": detail or None})

        event("start", "开始生产", iso(mash.started_at) or iso(batch.started_at), mash.assignee_id.name if mash else None)
        for index, node in enumerate(nodes):
            if node.get("state") == "done":
                value = node.get("actual_quantity", node.get("value"))
                event("mash-" + str(index), node.get("title") or "糖化工序", node.get("completed_at"),
                      node.get("completed_by"), f"{value} {node.get('unit') or ''}".strip() if value not in (None, "") else None)
        event("tank", "酒液进罐", iso(batch.entered_tank_at), None, plan.tank_id.name)
        event("fermentation", "开始发酵", (ferm or {}).get("started_at") or iso(fermentation.started_at), fermentation.assignee_id.name if fermentation else None)
        event("brewed", "酿造完成", completion.get("confirmed_at") or completion.get("completed_at") or iso(batch.completed_at) or iso(fermentation.completed_at),
              completion.get("confirmed_by") or completion.get("completed_by") or ((ferm or {}).get("completion") or {}).get("by"))
        if linked and (data or packing):
            event("pack-start", "开始包装", iso(packing.started_at), packing.assignee_id.name if packing else None)
            event("pack-end", "包装完成", execution.get("completed_at") or iso(packing.completed_at), execution.get("completed_by"), execution.get("notes"))
        for order in orders:
            event("order-start-" + str(order.id), "开始包装 · " + order.name, iso(order.started_at), order.task_id.assignee_id.name or None)
            event("order-end-" + str(order.id), "包装完成 · " + order.name, iso(order.submitted_at), order.task_id.assignee_id.name or None, f"{order.output_qty:g} 件" if order.submitted_at else None)
            event("release-" + str(order.id), "成品放行", iso(order.released_at), order.released_by_id.name, order.finished_lot_id.name)
        materials = []
        for actual in batch.material_actual_ids:
            record = actual.production_record_id
            materials.append({"id": "actual-" + str(actual.id), "name": actual.product_id.with_context(display_default_code=False).display_name,
                              "lot": actual.lot_id.name, "quantity": actual.quantity, "unit": actual.uom_id.name,
                              "at": iso(record.recorded_at), "by": record.operator_id.name or None})
        requisitions = self.env["brew.material.requisition"].sudo().search([
            ("company_id", "=", self.company.id), ("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")])
        # Task-only production records material batches on acknowledged allocations.
        seen = {(a.product_id.id, a.lot_id.id) for a in batch.material_actual_ids}
        for request in requisitions:
            for line in request.line_ids:
                for allocation in line.allocation_ids:
                    if allocation.acknowledged_qty <= 0 or (line.product_id.id, allocation.lot_id.id) in seen:
                        continue
                    materials.append({"id": "allocation-" + str(allocation.id), "name": line.product_id.with_context(display_default_code=False).display_name,
                                      "lot": allocation.lot_id.name, "quantity": allocation.acknowledged_qty,
                                      "unit": line.uom_id.name, "at": iso(request.acknowledged_at), "by": None, "basis": "实领数量"})
        return {"product": packaging_data(package) if package else self.product_data(batch), "batch": self._summary(batch, package),
                "tank": plan.tank_id.name, "recipe": plan.recipe_version_id.name,
                "events": events, "materials": materials}
