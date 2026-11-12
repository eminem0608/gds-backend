import json
from datetime import timedelta

from odoo import fields

from ..controllers.base import ApiError
from .inventory import InventoryService
from .tank_context import tank_plan, tank_stage, task_measurements, tank_measurements
from .task_visibility import unique_review_tasks


REPORT_GROUPS = {
    "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_brewery_manager",
    "craft_brew_core.group_brew_warehouse", "craft_brew_core.group_brew_purchase",
    "craft_brew_core.group_brew_sales", "craft_brew_core.group_brew_finance",
    "craft_brew_core.group_brew_admin",
}


class ReportingService:
    def __init__(self, env, user, company, enforce_report_access=True):
        self.env, self.user, self.company = env, user, company
        if enforce_report_access and not any(user.has_group(group) for group in REPORT_GROUPS):
            raise ApiError("PERMISSION_DENIED", "The current account cannot view dashboards or reports.", 403)
        self.inventory = InventoryService(env, user, company, enforce_access=False)

    def _factory_ids(self, requested=None):
        allowed = self.inventory._allowed_factory_ids()
        if requested:
            value = int(requested)
            if value not in allowed:
                raise ApiError("DATA_SCOPE_DENIED", "Factory is outside the current account scope.", 403)
            return [value]
        return allowed

    def dashboard(self, factory_id=None):
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director",
            "craft_brew_core.group_brew_brewery_manager", "craft_brew_core.group_brew_operator", "craft_brew_core.group_brew_warehouse",
            "craft_brew_core.group_brew_admin", "craft_brew_core.group_brew_purchase",
            "craft_brew_core.group_brew_sales", "craft_brew_core.group_brew_finance",
        }):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看总数据看板。", 403)
        factory_ids = self._factory_ids(factory_id)
        task_domain = [("company_id", "=", self.company.id), ("state", "not in", ["done", "cancelled"]), "|", ("factory_id", "=", False), ("factory_id", "in", factory_ids)]
        if not any(self.user.has_group(group) for group in {
            "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_production_director", "craft_brew_core.group_brew_admin"
        }):
            task_domain += ["|", ("assignee_id", "=", self.user.id), "&", ("assignee_id", "=", False), ("role_code", "in", self.user.brew_role_codes())]
        tanks = self.env["brew.tank"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("active", "=", True)])
        anomalies = self.env["brew.production.anomaly"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "!=", "closed")], order="detected_at desc,id desc", limit=10)
        sessions = self.env["brew.fermentation.session"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "ready")])
        plans = self.env["brew.production.plan"].sudo()
        packaging = self.env["brew.packaging.order"].sudo()
        outbound = self.env["brew.outbound.order"].sudo()
        expiration_limit = fields.Date.today() + timedelta(days=30)
        locations = self.env["stock.location"].sudo().search([("brew_factory_id", "in", factory_ids), ("usage", "=", "internal")])
        scoped_lot_ids = self.env["stock.quant"].sudo().search([
            ("company_id", "=", self.company.id), ("location_id", "in", locations.ids),
            ("quantity", ">", 0), ("lot_id", "!=", False),
        ]).mapped("lot_id").ids
        lots = self.env["stock.lot"].sudo().search_count([
            ("id", "in", scoped_lot_ids), ("company_id", "=", self.company.id), ("brew_expiration_date", "!=", False),
            ("brew_expiration_date", ">=", fields.Date.today()), ("brew_expiration_date", "<=", expiration_limit), ("brew_quality_status", "!=", "rejected")
        ])
        result = {
            "metrics": {
                "open_tasks": len(unique_review_tasks(self.env["brew.task"].sudo().search(task_domain).filtered(self._task_in_scope), self.user.id)),
                "active_tanks": len(tanks.filtered(lambda item: item.status not in {"empty", "maintenance"})),
                "open_anomalies": self.env["brew.production.anomaly"].sudo().search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "!=", "closed")]),
                "ready_volume_l": sum(tanks.filtered(lambda tank: tank.status == "ready").mapped("current_volume_l")),
                "plans_pending": plans.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "in", ["draft", "checked", "pending_approval", "approved"])]),
                "packaging_pending": packaging.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "in", ["draft", "in_progress", "pending_release"])]),
                "outbound_pending": outbound.search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "draft")]),
                "expiring_lots": lots,
                "unread_notifications": self.env["brew.notification"].sudo().search_count([("company_id", "=", self.company.id), ("user_id", "=", self.user.id), ("read_at", "=", False)]),
            },
            "tanks": [{
                "id": str(tank.id), "name": tank.name, "factory": tank.factory_id.name,
                "status": tank.status, "cleaning_status": tank.cleaning_status,
                "product": tank.current_product, "batch": tank.current_batch_no,
                "volume_l": tank.current_volume_l, "capacity_l": tank.capacity_l,
                "temperature_c": tank.current_temperature, "pressure_bar": tank.current_pressure,
                "gravity_plato": tank.current_gravity,
            } for tank in tanks],
            "alerts": [{
                "id": str(item.id), "type": item.anomaly_type, "severity": item.severity,
                "state": item.state, "session": item.session_id.name, "tank": item.session_id.tank_id.name,
                "detected_at": item.detected_at,
            } for item in anomalies],
        }

        for item, tank in zip(result['tanks'], tanks):
            plan = tank_plan(self.env, tank)
            stage, label = tank_stage(self.env, tank, plan)
            item.update(stage=stage, stage_label=label)
            measurements = tank_measurements(self.env, tank, plan)
            for key, field in [('temperature', 'temperature_c'), ('pressure', 'pressure_bar'), ('gravity', 'gravity_plato')]:
                if key in measurements:
                    item[field] = measurements[key]['value']
                else:
                    item[field] = None
            if plan and not tank.current_product:
                item['product'] = plan.product_tmpl_id.name
        from .stock_browser import StockBrowserService
        liquids = StockBrowserService(self.env,self.user,self.company,enforce_access=False).production_liquids({'factory_id':factory_id} if factory_id else {})
        completed_liquids = [r for r in liquids if r['production_status'] in {'raw_ready','awaiting_packaging'}]
        result['metrics']['ready_volume_l'] = sum(r['volume_l'] for r in completed_liquids)
        pending_locations = sum(not r.get('tank_verified') for r in completed_liquids)
        for item, tank in zip(result['tanks'],tanks):
            candidates = [r for r in completed_liquids if r.get('tank_id') == str(tank.id)]
            if candidates and not tank.current_production_batch_id and tank.current_volume_l <= 0:
                item.update(status='abnormal', stage_label='原液待核对',
                            unverified_volume_l=sum(r['volume_l'] for r in candidates),
                            product=candidates[0]['product']['name'], batch=candidates[0]['batch_name'])
        result['metrics']['raw_location_pending'] = pending_locations
        result.update(self._dashboard_details(factory_ids, task_domain, scoped_lot_ids))
        if not result['sections']['production']:
            result['tanks'] = []
            result['alerts'] = []
            for key in ('active_tanks', 'open_anomalies', 'ready_volume_l', 'raw_location_pending', 'plans_pending', 'packaging_pending'):
                result['metrics'].pop(key, None)
        if not result['sections']['inventory']:
            result['metrics'].pop('expiring_lots', None)
        return result

    def _task_in_scope(self, task):
        from .workflow import WorkflowService
        return WorkflowService(self.env, self.user, self.company).record_in_scope(task)

    def _dashboard_details(self, factory_ids, task_domain, lot_ids):
        def role(name):
            return self.user.has_group('craft_brew_core.group_brew_' + name)
        manager = any(role(name) for name in ('admin', 'general_manager', 'production_director', 'brewery_manager'))
        sections = {'production': manager or role('operator') or role('warehouse'),
                    'inventory': manager or role('warehouse') or role('purchase') or role('finance'),
                    'purchase': manager or role('warehouse') or role('purchase') or role('finance'),
                    'delivery': manager or role('sales')}
        today = fields.Date.context_today(self.user.with_context(tz=self.user.tz or 'Asia/Shanghai'))
        now = fields.Datetime.now()
        scope = [('company_id', '=', self.company.id), ('factory_id', 'in', factory_ids)]
        tasks = self.env['brew.task'].sudo()
        task_rows = unique_review_tasks(tasks.search(task_domain, order='due_at asc,id desc').filtered(self._task_in_scope), self.user.id)
        result = {'sections': sections, 'as_of': now.isoformat(),
                  'factories': [{'id': str(f.id), 'name': f.name} for f in self.env['brew.factory'].sudo().browse(self._factory_ids())],
                  'tasks': [{'id': str(t.id), 'name': t.name, 'assignee': t.assignee_id.name or '待领取',
                             'due_at': t.due_at.isoformat() if t.due_at else None,
                             'overdue': bool(t.due_at and t.due_at < now)} for t in task_rows[:6]],
                  'overdue_tasks': sum(bool(t.due_at and t.due_at < now) for t in task_rows)}
        if sections['production']:
            plan_domain = scope + [('state', 'not in', ['cancelled', 'terminated'])]
            if role('operator') and not manager and not role('warehouse'):
                plan_domain += ['|', '|', ('mash_owner_id', '=', self.user.id), ('fermentation_owner_id', '=', self.user.id), ('packaging_owner_id', '=', self.user.id)]
            plans = self.env['brew.production.plan'].sudo().search(plan_domain, order='id desc')
            execution_tasks = tasks.search([('company_id', '=', self.company.id), ('target_model', '=', 'brew.production.plan'),
                ('target_id', 'in', plans.ids), ('task_type', 'in', ['mashing', 'fermentation', 'packaging']), ('state', '!=', 'cancelled')])
            grouped = {}
            for task in execution_tasks:
                grouped.setdefault(task.target_id, {})[task.task_type] = task
            labels = [('stocking', '原料备货'), ('dispatch', '待下发'), ('mashing', '糖化执行'),
                      ('fermentation', '发酵执行'), ('packaging', '待包装'), ('packing', '包装执行'), ('done', '生产完成')]
            counts = dict.fromkeys((key for key, _ in labels), 0)
            factory_month = {factory_id: {'volume_l': 0.0, 'batches': 0, 'active': 0} for factory_id in factory_ids}
            factory_names = {factory.id: factory.name for factory in self.env['brew.factory'].sudo().browse(factory_ids)}
            recent = []
            for plan in plans:
                task_map = grouped.get(plan.id, {})
                mash, ferment = task_map.get('mashing'), task_map.get('fermentation')
                package = json.loads(plan.packaging_plan_json or '{}')
                packing = task_map.get('packaging')
                brewing_done = plan.state == 'done' or (mash and mash.state == 'done' and ferment and ferment.state == 'done')
                stage = 'done' if brewing_done else 'stocking' if plan.state == 'stocking' else 'dispatch' if plan.state not in {'dispatched', 'in_progress'} else 'mashing' if not mash or mash.state != 'done' else 'fermentation'
                if brewing_done and package and not (package.get('status') == 'packaged' or package.get('execution') or packing and packing.state == 'done'):
                    stage = 'packing' if packing and packing.state == 'in_progress' else 'packaging'

                counts[stage] += 1
                factory_id = plan.factory_id.id
                if factory_id in factory_month and stage in {'mashing', 'fermentation', 'packaging', 'packing'}:
                    factory_month[factory_id]['active'] += 1
                if len(recent) < 6:
                    recent.append({'id': str(plan.id), 'name': plan.name, 'product': plan.product_tmpl_id.name,
                        'factory': plan.factory_id.name, 'tank': plan.tank_id.name, 'volume_l': plan.planned_volume_l,
                        'stage': stage, 'stage_label': dict(labels)[stage],
                        'owner': (plan.packaging_owner_id.name if stage in {'packaging', 'packing'} else plan.fermentation_owner_id.name if stage == 'fermentation' else plan.mash_owner_id.name) or '待分配'})
            batches = self.env['brew.production.batch'].sudo().search(scope + [('production_plan_id', 'in', plans.ids), ('state', '=', 'completed'), ('completed_at', '!=', False)])
            def local_day(value):
                return fields.Datetime.context_timestamp(self.user.with_context(tz=self.user.tz or 'Asia/Shanghai'), value).date()
            days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
            output = {day: 0.0 for day in days}
            history_days = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
            factory_history = {factory_id: {day: 0.0 for day in history_days} for factory_id in factory_ids}
            month_volume, month_batches = 0.0, 0
            for batch in batches:
                day = local_day(batch.completed_at)
                if day in output: output[day] += batch.actual_volume_l
                factory_id = batch.production_plan_id.factory_id.id
                if day in history_days and factory_id in factory_history:
                    factory_history[factory_id][day] += batch.actual_volume_l
                if today.replace(day=1) <= day <= today:
                    month_volume += batch.actual_volume_l
                    month_batches += 1
                    if factory_id in factory_month:
                        factory_month[factory_id]['volume_l'] += batch.actual_volume_l
                        factory_month[factory_id]['batches'] += 1
            result['production'] = {'stages': [{'key': key, 'label': label, 'count': counts[key]} for key, label in labels],
                'active': counts['mashing'] + counts['fermentation'] + counts['packaging'] + counts['packing'], 'recent': recent,
                'month_volume_l': month_volume, 'month_batches': month_batches,
                'factory_summaries': [{'id': str(factory_id), 'name': factory_names[factory_id], **values}
                    for factory_id, values in factory_month.items()],
                'factory_trends': [{'id': str(factory_id), 'name': factory_names[factory_id],
                    'trend': [{'date': day.isoformat(), 'volume_l': factory_history[factory_id][day]} for day in history_days]}
                    for factory_id in factory_ids],
                'trend': [{'date': day.isoformat(), 'volume_l': output[day]} for day in days]}
        if sections['inventory']:
            lots = self.env['stock.lot'].sudo()
            lot_scope = [('company_id', '=', self.company.id), ('id', 'in', lot_ids)]
            result['inventory'] = {
                'expired': lots.search_count(lot_scope + [('brew_expiration_date', '<', today)]),
                'expiring': lots.search_count(lot_scope + [('brew_expiration_date', '>=', today), ('brew_expiration_date', '<=', today + timedelta(days=30))]),
                'pending_quality': lots.search_count(lot_scope + [('brew_quality_status', '=', 'pending')]),
                'frozen': lots.search_count(lot_scope + [('brew_frozen', '=', True)])}
        if sections['purchase']:
            orders = self.env['purchase.order'].sudo().search([('company_id', '=', self.company.id), ('brew_factory_id', 'in', factory_ids), ('state', '=', 'purchase')])
            result['purchase'] = {
                'requisition_pending': self.env['brew.purchase.requisition'].sudo().search_count(scope + [('state', '=', 'submitted')]),
                'order_pending': self.env['purchase.order'].sudo().search_count([('company_id', '=', self.company.id), ('brew_factory_id', 'in', factory_ids), ('brew_approval_state', '=', 'pending'), ('state', '!=', 'cancel')]),
                'awaiting_receipt': len(orders.filtered(lambda order: any(line.product_qty - line.qty_received > line.product_uom.rounding / 2 for line in order.order_line if not line.display_type))),
                'awaiting_quality': self.env['brew.receipt'].sudo().search_count(scope + [('state', '=', 'arrived')]),
                'awaiting_stock': self.env['brew.receipt'].sudo().search_count(scope + [('state', '=', 'inspected')])}
        if sections['delivery']:
            domain = [('company_id', '=', self.company.id), '|', ('factory_id', '=', False), ('factory_id', 'in', factory_ids),
                      ('state', 'in', ['submitted', 'confirmed', 'planned'])]
            if role('sales') and not manager: domain += [('salesperson_id', '=', self.user.id)]
            demands = self.env['brew.product.demand'].sudo()
            upcoming_domain = domain + [('requested_delivery_date', '>=', today), ('requested_delivery_date', '<=', today + timedelta(days=7))]
            upcoming = demands.search(upcoming_domain, order='requested_delivery_date,id', limit=6)
            result['delivery'] = {'pending_confirmation': demands.search_count(domain + [('state', '=', 'submitted')]),
                'awaiting_plan': demands.search_count(domain + [('state', '=', 'confirmed')]),
                'upcoming_count': demands.search_count(upcoming_domain),
                'upcoming': [{'id': str(d.id), 'name': d.name, 'product': d.product_tmpl_id.name,
                    'date': d.requested_delivery_date.isoformat(), 'volume_l': d.requested_volume_l,
                    'state': {'submitted': '待确认', 'confirmed': '待排产', 'planned': '已排产'}[d.state]} for d in upcoming]}
        return result

    def report_summary(self, factory_id=None):
        factory_ids = self._factory_ids(factory_id)
        released = self.env["brew.packaging.order"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "released")])
        purchases = self.env["purchase.order"].sudo().search([("company_id", "=", self.company.id), ("brew_factory_id", "in", factory_ids), ("state", "in", ["purchase", "done"])])
        outbound = self.env["brew.outbound.order"].sudo().search([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "=", "shipped")])
        all_reports = any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"})
        production_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_brewery_manager")
        purchase_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_purchase") or self.user.has_group("craft_brew_core.group_brew_finance")
        outbound_allowed = all_reports or self.user.has_group("craft_brew_core.group_brew_sales") or self.user.has_group("craft_brew_core.group_brew_warehouse")
        return {
            "production": {"released_batches": len(released) if production_allowed else 0, "output_volume_l": sum(released.mapped("output_volume_l")) if production_allowed else 0, "estimated_cost": sum(released.mapped("estimated_total_cost")) if production_allowed else 0},
            "purchase": {"orders": len(purchases) if purchase_allowed else 0, "amount_total": sum(purchases.mapped("amount_total")) if purchase_allowed else 0},
            "outbound": {"orders": len(outbound) if outbound_allowed else 0, "lines": len(outbound.mapped("line_ids")) if outbound_allowed else 0},
            "quality": {"open_anomalies": self.env["brew.production.anomaly"].sudo().search_count([("company_id", "=", self.company.id), ("factory_id", "in", factory_ids), ("state", "!=", "closed")]) if production_allowed else 0},
        }

    def _allowed_export_types(self):
        if any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_finance", "craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"}):
            return {"purchase", "inbound", "outbound", "inventory"}
        result = set()
        if self.user.has_group("craft_brew_core.group_brew_purchase"): result.add("purchase")
        if self.user.has_group("craft_brew_core.group_brew_warehouse"): result.update({"inbound", "outbound", "inventory"})
        if self.user.has_group("craft_brew_core.group_brew_sales"): result.add("outbound")
        return result

    def _serialize_export(self, record):
        return {
            "id": str(record.id), "name": record.name, "export_type": record.export_type,
            "state": record.state, "filters": json.loads(record.filters_json or "{}"),
            "factory_id": str(record.factory_id.id) if record.factory_id else None,
            "row_count": record.row_count, "error_message": record.error_message,
            "created_at": record.create_date, "completed_at": record.completed_at,
            "download_url": f"/api/v1/exports/{record.id}/download" if record.state == "done" else None,
        }

    def exports(self, page, page_size):
        domain = [("company_id", "=", self.company.id)]
        if not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"}):
            domain.append(("requested_by_id", "=", self.user.id))
        model = self.env["brew.export.job"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [self._serialize_export(record) for record in records], total

    def create_export(self, payload):
        export_type = payload.get("export_type")
        if export_type not in self._allowed_export_types():
            raise ApiError("PERMISSION_DENIED", "This account cannot create the requested export type.", 403)
        factory = self.inventory._factory(payload.get("factory_id")) if payload.get("factory_id") else None
        filters = payload.get("filters") or {}
        if not isinstance(filters, dict):
            raise ApiError("VALIDATION_ERROR", "Export filters must be an object.", 422)
        record = self.env["brew.export.job"].sudo().create({
            "company_id": self.company.id, "factory_id": factory.id if factory else False,
            "requested_by_id": self.user.id, "export_type": export_type,
            "filters_json": json.dumps(filters, ensure_ascii=False, sort_keys=True),
        })
        return record, self._serialize_export(record)

    def export_record(self, record_id):
        record = self.env["brew.export.job"].sudo().search([("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id)], limit=1)
        if not record or (record.requested_by_id != self.user and not any(self.user.has_group(group) for group in {"craft_brew_core.group_brew_general_manager", "craft_brew_core.group_brew_admin"})):
            raise ApiError("RESOURCE_NOT_FOUND", "Export job does not exist.", 404)
        return record

    def notifications(self, page, page_size, unread_only=False):
        domain = [("company_id", "=", self.company.id), ("user_id", "=", self.user.id)]
        if unread_only: domain.append(("read_at", "=", False))
        model = self.env["brew.notification"].sudo(); total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="create_date desc,id desc")
        return [{"id": str(item.id), "type": item.notification_type, "title": item.title, "body": item.body, "read_at": item.read_at, "created_at": item.create_date} for item in records], total

    def read_notification(self, record_id):
        record = self.env["brew.notification"].sudo().search([("id", "=", int(record_id or 0)), ("company_id", "=", self.company.id), ("user_id", "=", self.user.id)], limit=1)
        if not record: raise ApiError("RESOURCE_NOT_FOUND", "Notification does not exist.", 404)
        record.action_read()
        return record, {"id": str(record.id), "read_at": record.read_at}

    def preference(self):
        record = self.env["brew.notification.preference"].sudo().search([("user_id", "=", self.user.id), ("company_id", "=", self.company.id)], limit=1)
        if not record:
            record = self.env["brew.notification.preference"].sudo().create({"user_id": self.user.id, "company_id": self.company.id})
        return record

    @staticmethod
    def serialize_preference(record):
        return {key: record[key] for key in ["in_app_enabled", "email_enabled", "wechat_enabled", "anomaly_enabled", "task_enabled", "export_enabled", "minimum_severity"]}

    def update_preference(self, payload):
        record = self.preference()
        allowed = {"in_app_enabled", "email_enabled", "wechat_enabled", "anomaly_enabled", "task_enabled", "export_enabled", "minimum_severity"}
        record.write({key: value for key, value in payload.items() if key in allowed})
        return record, self.serialize_preference(record)

    def operations_metrics(self):
        if not self.user.has_group("craft_brew_core.group_brew_admin"):
            raise ApiError("PERMISSION_DENIED", "Administrator permission is required.", 403)
        return {
            "outbox": {state: self.env["brew.outbox.event"].sudo().search_count([("company_id", "=", self.company.id), ("state", "=", state)]) for state in ["pending", "processing", "published", "failed"]},
            "exports": {state: self.env["brew.export.job"].sudo().search_count([("company_id", "=", self.company.id), ("state", "=", state)]) for state in ["queued", "running", "done", "failed"]},
            "active_sessions": self.env["brew.api.session"].sudo().search_count([("company_id", "=", self.company.id), ("revoked_at", "=", False)]),
        }
