import base64
import csv
import io
import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError


EXPORT_TYPES = {"purchase", "inbound", "outbound", "inventory"}


class BrewExportJob(models.Model):
    _name = "brew.export.job"
    _description = "Craft Brew Asynchronous Export Job"
    _order = "create_date desc,id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    requested_by_id = fields.Many2one("res.users", required=True, check_company=True, index=True)
    export_type = fields.Selection(
        [("purchase", "Purchase"), ("inbound", "Inbound"), ("outbound", "Outbound"), ("inventory", "Inventory movements")],
        required=True, index=True,
    )
    filters_json = fields.Text(required=True, default="{}")
    state = fields.Selection(
        [("queued", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")],
        required=True, default="queued", index=True,
    )
    attachment_id = fields.Many2one("ir.attachment", readonly=True, ondelete="set null")
    row_count = fields.Integer(readonly=True)
    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    error_message = fields.Text(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("export_type") not in EXPORT_TYPES:
                raise ValidationError("Only purchase, inbound, outbound and inventory exports are allowed.")
            try:
                filters = json.loads(values.get("filters_json") or "{}")
            except (TypeError, ValueError) as exc:
                raise ValidationError("Export filters must be valid JSON.") from exc
            if not isinstance(filters, dict):
                raise ValidationError("Export filters must be a JSON object.")
            if values.get("name", "New") == "New":
                values["name"] = self.env["ir.sequence"].next_by_code("brew.export.job") or "New"
        return super().create(values_list)

    def _date_domain(self, field_name, filters):
        domain = []
        if filters.get("date_from"):
            domain.append((field_name, ">=", filters["date_from"]))
        if filters.get("date_to"):
            domain.append((field_name, "<=", filters["date_to"] + " 23:59:59"))
        return domain

    def _purchase_rows(self, filters):
        domain = [("company_id", "=", self.company_id.id)] + self._date_domain("date_order", filters)
        if self.factory_id:
            domain.append(("brew_factory_id", "=", self.factory_id.id))
        records = self.env["purchase.order"].sudo().search(domain, order="date_order,id")
        header = ["order_no", "supplier", "order_date", "state", "amount_total", "currency"]
        rows = [[item.name, item.partner_id.display_name, item.date_order, item.state, item.amount_total, item.currency_id.name] for item in records]
        return header, rows

    def _movement_rows(self, filters, direction=None):
        domain = [("company_id", "=", self.company_id.id), ("state", "=", "done"), ("move_id.brew_controlled", "=", True)]
        domain += self._date_domain("date", filters)
        if self.factory_id:
            locations = self.env["stock.location"].sudo().search([("brew_factory_id", "=", self.factory_id.id)])
            domain += ["|", ("location_id", "in", locations.ids), ("location_dest_id", "in", locations.ids)]
        if direction == "inbound":
            domain += [("location_id.usage", "!=", "internal"), ("location_dest_id.usage", "=", "internal")]
        elif direction == "outbound":
            domain += [("location_id.usage", "=", "internal"), ("location_dest_id.usage", "!=", "internal")]
        records = self.env["stock.move.line"].sudo().search(domain, order="date,id")
        header = ["date", "reference", "product", "lot", "quantity", "uom", "source", "destination", "source_document"]
        rows = [[
            item.date, item.move_id.reference, item.product_id.display_name, item.lot_id.name or "",
            item.quantity, item.product_uom_id.name, item.location_id.complete_name,
            item.location_dest_id.complete_name, item.move_id.brew_source_reference,
        ] for item in records]
        return header, rows

    def _generate_csv(self):
        self.ensure_one()
        filters = json.loads(self.filters_json or "{}")
        if self.export_type == "purchase":
            header, rows = self._purchase_rows(filters)
        elif self.export_type == "inbound":
            header, rows = self._movement_rows(filters, "inbound")
        elif self.export_type == "outbound":
            header, rows = self._movement_rows(filters, "outbound")
        else:
            header, rows = self._movement_rows(filters)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
        return ("\ufeff" + stream.getvalue()).encode("utf-8"), len(rows)

    def action_process(self):
        for job in self:
            if job.state != "queued":
                continue
            job.write({"state": "running", "started_at": fields.Datetime.now(), "error_message": False})
            try:
                content, row_count = job._generate_csv()
                attachment = self.env["ir.attachment"].sudo().create({
                    "name": f"{job.name}-{job.export_type}.csv", "type": "binary",
                    "datas": base64.b64encode(content), "mimetype": "text/csv",
                    "res_model": job._name, "res_id": job.id,
                })
                job.write({"state": "done", "attachment_id": attachment.id, "row_count": row_count, "completed_at": fields.Datetime.now()})
                self.env["brew.notification"].sudo().notify(
                    job.requested_by_id, job.company_id, "export_ready", "Export ready",
                    f"{job.name} completed with {row_count} rows.", job._name, job.id,
                )
                self.env["brew.outbox.event"].sudo().enqueue(
                    "export.ready", job._name, job.id, {"job_id": job.id, "type": job.export_type, "rows": row_count}, job.company_id
                )
            except Exception as exc:
                job.write({"state": "failed", "error_message": str(exc)[:4000], "completed_at": fields.Datetime.now()})
        return True

    @api.model
    def _cron_process(self, batch_size=10):
        jobs = self.sudo().search([("state", "=", "queued")], order="id", limit=batch_size)
        jobs.action_process()
        return len(jobs)


class BrewNotificationPreference(models.Model):
    _name = "brew.notification.preference"
    _description = "Craft Brew Notification Preference"
    _check_company_auto = True

    user_id = fields.Many2one("res.users", required=True, check_company=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    in_app_enabled = fields.Boolean(default=True)
    email_enabled = fields.Boolean(default=False)
    wechat_enabled = fields.Boolean(default=False)
    anomaly_enabled = fields.Boolean(default=True)
    task_enabled = fields.Boolean(default=True)
    export_enabled = fields.Boolean(default=True)
    minimum_severity = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")],
        required=True, default="medium",
    )

    _sql_constraints = [("user_company_unique", "unique(user_id,company_id)", "Notification preferences must be unique per user and company.")]


class BrewNotification(models.Model):
    _name = "brew.notification"
    _description = "Craft Brew In-App Notification"
    _order = "create_date desc,id desc"
    _check_company_auto = True

    user_id = fields.Many2one("res.users", required=True, check_company=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    notification_type = fields.Char(required=True, index=True)
    title = fields.Char(required=True)
    body = fields.Text(required=True)
    source_model = fields.Char(index=True)
    source_id = fields.Integer(index=True)
    read_at = fields.Datetime(index=True)

    _sql_constraints = [
        ("user_source_unique", "unique(user_id,notification_type,source_model,source_id)", "Duplicate source notification."),
    ]

    @api.model
    def notify(self, user, company, notification_type, title, body, source_model=None, source_id=0):
        preference = self.env["brew.notification.preference"].sudo().search([
            ("user_id", "=", user.id), ("company_id", "=", company.id)
        ], limit=1)
        if preference and not preference.in_app_enabled:
            return self.browse()
        existing = self.sudo().search([
            ("user_id", "=", user.id), ("notification_type", "=", notification_type),
            ("source_model", "=", source_model or False), ("source_id", "=", int(source_id or 0)),
        ], limit=1)
        if existing:
            return existing
        return self.sudo().create({
            "user_id": user.id, "company_id": company.id, "notification_type": notification_type,
            "title": title, "body": body, "source_model": source_model, "source_id": int(source_id or 0),
        })

    def action_read(self):
        self.filtered(lambda item: not item.read_at).write({"read_at": fields.Datetime.now()})
        return True


class BrewProductionAnomaly(models.Model):
    _inherit = "brew.production.anomaly"

    @api.model
    def detect_or_merge(self, session, anomaly_type, severity, metrics):
        anomaly = super().detect_or_merge(session, anomaly_type, severity, metrics)
        users = anomaly.assignee_id
        if not users:
            group = self.env.ref("craft_brew_core.group_brew_production_director")
            users = group.users.filtered(lambda user: session.company_id in user.company_ids and user.active)
        ranks = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for user in users:
            preference = self.env["brew.notification.preference"].sudo().search([
                ("user_id", "=", user.id), ("company_id", "=", session.company_id.id)
            ], limit=1)
            if preference and (
                not preference.anomaly_enabled
                or ranks[severity] < ranks[preference.minimum_severity]
            ):
                continue
            self.env["brew.notification"].sudo().notify(
                user, session.company_id, "production_anomaly",
                f"Production anomaly: {anomaly_type}",
                f"{session.name} / {session.tank_id.name} / severity {severity}",
                anomaly._name, anomaly.id,
            )
        return anomaly


class BrewTask(models.Model):
    _inherit = "brew.task"

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list)
        records._notify_assignees()
        return records

    def write(self, values):
        result = super().write(values)
        if "assignee_id" in values and values.get("assignee_id"):
            self._notify_assignees()
        return result

    def _notify_assignees(self):
        for task in self.filtered("assignee_id"):
            preference = self.env["brew.notification.preference"].sudo().search([
                ("user_id", "=", task.assignee_id.id), ("company_id", "=", task.company_id.id)
            ], limit=1)
            if preference and not preference.task_enabled:
                continue
            self.env["brew.notification"].sudo().notify(
                task.assignee_id, task.company_id, "task_assigned", "Task assigned",
                task.name, task._name, task.id,
            )
