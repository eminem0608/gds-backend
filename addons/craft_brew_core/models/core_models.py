import json
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.role_service import ROLE_DEFINITIONS, default_page_for_roles, menus_for_roles, permissions_for_roles
from ..services.security_utils import canonical_json_hash, generate_token, hash_token, verify_token


ACCESS_TOKEN_MINUTES = 30
REFRESH_TOKEN_DAYS = 30


class ResUsers(models.Model):
    _inherit = "res.users"

    brew_session_version = fields.Integer(default=1, copy=False)
    brew_api_session_ids = fields.One2many("brew.api.session", "user_id", string="API 会话")

    def write(self, values):
        revoke_sessions = "active" in values and not values["active"]
        result = super().write(values)
        if revoke_sessions:
            self.sudo().mapped("brew_api_session_ids").action_revoke("user_disabled")
        return result

    def brew_role_codes(self):
        self.ensure_one()
        return [
            code
            for code, definition in ROLE_DEFINITIONS.items()
            if self.has_group(definition["xml_id"])
        ]

    def brew_context_payload(self):
        self.ensure_one()
        role_codes = self.brew_role_codes()
        companies = self.company_ids.filtered("active")
        return {
            "user": {"id": str(self.id), "name": self.name, "login": self.login},
            "roles": [
                {"code": code, "label": ROLE_DEFINITIONS[code]["label"]}
                for code in role_codes
            ],
            "companies": [
                {"id": str(company.id), "name": company.name}
                for company in companies
            ],
            "current_company_id": str(self.env.company.id),
            "factories": [],
            "default_factory_id": None,
            "permissions": permissions_for_roles(role_codes),
            "menus": menus_for_roles(role_codes),
            "default_page": default_page_for_roles(role_codes),
        }


class BrewApiSession(models.Model):
    _name = "brew.api.session"
    _description = "Craft Brew API Session"
    _order = "create_date desc"
    _rec_name = "device_name"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    access_token_hash = fields.Char(required=True, index=True, copy=False)
    refresh_token_hash = fields.Char(required=True, index=True, copy=False)
    access_expires_at = fields.Datetime(required=True, index=True)
    refresh_expires_at = fields.Datetime(required=True, index=True)
    session_version = fields.Integer(required=True, copy=False)
    device_name = fields.Char(default="Unknown device")
    device_id = fields.Char(index=True)
    user_agent = fields.Char()
    ip_address = fields.Char()
    last_seen_at = fields.Datetime()
    revoked_at = fields.Datetime(index=True)
    revoked_reason = fields.Char()

    _sql_constraints = [
        ("access_token_hash_unique", "unique(access_token_hash)", "Access token hash must be unique."),
        ("refresh_token_hash_unique", "unique(refresh_token_hash)", "Refresh token hash must be unique."),
    ]

    @api.model
    def issue(self, user, company, device=None):
        if not user.active or company not in user.company_ids:
            raise AccessError("账号不可用或无权访问所选企业。")
        access_token = generate_token()
        refresh_token = generate_token()
        now = fields.Datetime.now()
        device = device or {}
        session = self.sudo().create(
            {
                "user_id": user.id,
                "company_id": company.id,
                "access_token_hash": hash_token(access_token),
                "refresh_token_hash": hash_token(refresh_token),
                "access_expires_at": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
                "refresh_expires_at": now + timedelta(days=REFRESH_TOKEN_DAYS),
                "session_version": user.brew_session_version,
                "device_name": device.get("name") or "Unknown device",
                "device_id": device.get("id"),
                "user_agent": device.get("user_agent"),
                "ip_address": device.get("ip_address"),
                "last_seen_at": now,
            }
        )
        return session, access_token, refresh_token

    @api.model
    def authenticate_access_token(self, token):
        token_hash = hash_token(token)
        if not token_hash:
            return self.browse()
        session = self.sudo().search(
            [("access_token_hash", "=", token_hash), ("revoked_at", "=", False)], limit=1
        )
        if not session or not verify_token(token, session.access_token_hash):
            return self.browse()
        now = fields.Datetime.now()
        if (
            session.access_expires_at <= now
            or session.refresh_expires_at <= now
            or not session.user_id.active
            or session.session_version != session.user_id.brew_session_version
            or session.company_id not in session.user_id.company_ids
        ):
            session.action_revoke("expired_or_invalid")
            return self.browse()
        session.write({"last_seen_at": now})
        return session

    @api.model
    def rotate(self, refresh_token):
        token_hash = hash_token(refresh_token)
        session = self.sudo().search(
            [("refresh_token_hash", "=", token_hash), ("revoked_at", "=", False)], limit=1
        )
        now = fields.Datetime.now()
        if (
            not session
            or not verify_token(refresh_token, session.refresh_token_hash)
            or session.refresh_expires_at <= now
            or not session.user_id.active
            or session.session_version != session.user_id.brew_session_version
        ):
            if session:
                session.action_revoke("refresh_invalid")
            raise AccessError("刷新凭证无效或已过期。")
        new_access_token = generate_token()
        new_refresh_token = generate_token()
        session.write(
            {
                "access_token_hash": hash_token(new_access_token),
                "refresh_token_hash": hash_token(new_refresh_token),
                "access_expires_at": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
                "refresh_expires_at": now + timedelta(days=REFRESH_TOKEN_DAYS),
                "last_seen_at": now,
            }
        )
        return session, new_access_token, new_refresh_token

    def action_revoke(self, reason="logout"):
        now = fields.Datetime.now()
        active_sessions = self.filtered(lambda record: not record.revoked_at)
        if active_sessions:
            active_sessions.sudo().write({"revoked_at": now, "revoked_reason": reason})
        return True

    def token_payload(self, access_token, refresh_token):
        self.ensure_one()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_MINUTES * 60,
            "refresh_expires_in": REFRESH_TOKEN_DAYS * 24 * 60 * 60,
            "session_id": str(self.id),
        }


class BrewIdempotencyRecord(models.Model):
    _name = "brew.idempotency.record"
    _description = "Craft Brew Idempotency Record"
    _order = "create_date desc"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    key = fields.Char(required=True, index=True)
    method = fields.Char(required=True)
    path = fields.Char(required=True)
    request_hash = fields.Char(required=True)
    state = fields.Selection(
        [("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed")],
        required=True,
        default="processing",
        index=True,
    )
    response_status = fields.Integer()
    response_json = fields.Text()
    expires_at = fields.Datetime(required=True, index=True)

    _sql_constraints = [
        (
            "idempotency_scope_unique",
            "unique(company_id, user_id, method, path, key)",
            "Idempotency key already exists for this operation.",
        )
    ]

    @api.model
    def begin(self, company, user, method, path, key, payload):
        request_hash = canonical_json_hash(payload)
        existing = self.sudo().search(
            [
                ("company_id", "=", company.id),
                ("user_id", "=", user.id),
                ("method", "=", method.upper()),
                ("path", "=", path),
                ("key", "=", key),
                ("expires_at", ">", fields.Datetime.now()),
            ],
            limit=1,
        )
        if existing:
            if existing.request_hash != request_hash:
                raise ValidationError("IDEMPOTENCY_KEY_REUSED")
            if existing.state == "processing":
                raise ValidationError("IDEMPOTENCY_IN_PROGRESS")
            if existing.state == "failed":
                existing.sudo().write(
                    {
                        "state": "processing",
                        "response_status": False,
                        "response_json": False,
                        "expires_at": fields.Datetime.now() + timedelta(hours=24),
                    }
                )
                return existing, False
            return existing, existing.state == "completed"
        record = self.sudo().create(
            {
                "company_id": company.id,
                "user_id": user.id,
                "method": method.upper(),
                "path": path,
                "key": key,
                "request_hash": request_hash,
                "expires_at": fields.Datetime.now() + timedelta(hours=24),
            }
        )
        return record, False

    def complete(self, status, response):
        self.ensure_one()
        self.sudo().write(
            {
                "state": "completed",
                "response_status": status,
                "response_json": json.dumps(response, ensure_ascii=False, default=str),
            }
        )

    def fail(self, status, response):
        self.ensure_one()
        self.sudo().write(
            {
                "state": "failed",
                "response_status": status,
                "response_json": json.dumps(response, ensure_ascii=False, default=str),
            }
        )

    def stored_response(self):
        self.ensure_one()
        return self.response_status, json.loads(self.response_json or "{}")


class BrewAuditLog(models.Model):
    _name = "brew.audit.log"
    _description = "Craft Brew Audit Log"
    _order = "create_date desc"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", index=True, ondelete="set null")
    event_type = fields.Char(required=True, index=True)
    resource_model = fields.Char(index=True)
    resource_id = fields.Char(index=True)
    action = fields.Char(required=True, index=True)
    request_id = fields.Char(index=True)
    ip_address = fields.Char()
    user_agent = fields.Char()
    summary_json = fields.Text()

    @api.model
    def log_event(self, event_type, action, **values):
        company = values.pop("company", self.env.company)
        user = values.pop("user", self.env.user)
        summary = values.pop("summary", {})
        return self.sudo().create(
            {
                "company_id": company.id,
                "user_id": user.id if user else False,
                "event_type": event_type,
                "action": action,
                "summary_json": json.dumps(summary, ensure_ascii=False, default=str),
                **values,
            }
        )


class BrewOutboxEvent(models.Model):
    _name = "brew.outbox.event"
    _description = "Craft Brew Transactional Outbox Event"
    _order = "id"

    event_uuid = fields.Char(required=True, default=lambda self: generate_token(), index=True, copy=False)
    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    event_type = fields.Char(required=True, index=True)
    aggregate_model = fields.Char(required=True, index=True)
    aggregate_id = fields.Char(required=True, index=True)
    payload_json = fields.Text(required=True, default="{}")
    state = fields.Selection(
        [("pending", "Pending"), ("processing", "Processing"), ("published", "Published"), ("failed", "Failed")],
        required=True,
        default="pending",
        index=True,
    )
    attempts = fields.Integer(default=0)
    available_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    published_at = fields.Datetime()
    last_error = fields.Text()

    _sql_constraints = [("event_uuid_unique", "unique(event_uuid)", "Outbox event UUID must be unique.")]

    @api.model
    def enqueue(self, event_type, aggregate_model, aggregate_id, payload=None, company=None):
        company = company or self.env.company
        return self.create(
            {
                "company_id": company.id,
                "event_type": event_type,
                "aggregate_model": aggregate_model,
                "aggregate_id": str(aggregate_id),
                "payload_json": json.dumps(payload or {}, ensure_ascii=False, default=str),
            }
        )

    def _dispatch(self):
        """Extension hook for integrations that consume committed domain events."""
        self.ensure_one()
        return True

    @api.model
    def _cron_dispatch(self, batch_size=100):
        now = fields.Datetime.now()
        events = self.sudo().search(
            [
                ("state", "in", ["pending", "failed"]),
                ("available_at", "<=", now),
                ("attempts", "<", 10),
            ],
            order="id",
            limit=batch_size,
        )
        for event in events:
            next_attempt = event.attempts + 1
            try:
                with self.env.cr.savepoint():
                    event.write({"state": "processing", "attempts": next_attempt})
                    event._dispatch()
                    event.write(
                        {
                            "state": "published",
                            "published_at": fields.Datetime.now(),
                            "last_error": False,
                        }
                    )
            except Exception as exc:
                delay_minutes = min(2 ** min(event.attempts, 8), 240)
                event.write(
                    {
                        "state": "failed",
                        "attempts": next_attempt,
                        "last_error": str(exc)[:4000],
                        "available_at": fields.Datetime.now() + timedelta(minutes=delay_minutes),
                    }
                )
        return len(events)
