import json

from odoo import Command, fields

from ..controllers.base import ApiError
from .inventory import InventoryService


FERMENTATION_GROUPS = {
    "craft_brew_core.group_brew_general_manager",
    "craft_brew_core.group_brew_production_director",
    "craft_brew_core.group_brew_operator",
    "craft_brew_core.group_brew_warehouse",
    "craft_brew_core.group_brew_admin",
}


class FermentationService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not any(user.has_group(group) for group in FERMENTATION_GROUPS):
            raise ApiError("PERMISSION_DENIED", "当前账号无权查看发酵数据。", 403)
        self.inventory = InventoryService(env, user, company, enforce_access=False)

    def _require_operator(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_operator")
            or self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权执行发酵操作。", 403)

    def _require_director(self):
        if not (
            self.user.has_group("craft_brew_core.group_brew_production_director")
            or self.user.has_group("craft_brew_core.group_brew_general_manager")
            or self.user.has_group("craft_brew_core.group_brew_admin")
        ):
            raise ApiError("PERMISSION_DENIED", "当前账号无权审核发酵数据。", 403)

    @staticmethod
    def _version(record):
        return record.write_date.isoformat() if record.write_date else None

    def _check_version(self, record, expected):
        current = self._version(record)
        if not expected or current != expected:
            raise ApiError(
                "VERSION_CONFLICT",
                "数据版本不一致，请刷新后重试。",
                409,
                {"current_version": current},
            )

    def _record(self, model, record_id):
        record = self.env[model].sudo().search(
            [
                ("id", "=", int(record_id or 0)),
                ("company_id", "=", self.company.id),
            ],
            limit=1,
        )
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "发酵业务数据不存在。", 404)
        factory = record.factory_id
        if factory and factory.id not in self.inventory._allowed_factory_ids():
            raise ApiError("DATA_SCOPE_DENIED", "发酵业务数据不在当前账号范围内。", 403)
        return record

    def _session_for_tank(self, tank):
        return self.env["brew.fermentation.session"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("tank_id", "=", tank.id),
                ("state", "not in", ["completed", "terminated"]),
            ],
            limit=1,
        )

    def _serialize_tank(self, tank):
        session = self._session_for_tank(tank)
        reading = session.latest_reading_id if session else None
        anomalies = (
            session.anomaly_ids.filtered(lambda item: item.state != "closed")
            if session
            else self.env["brew.production.anomaly"]
        )
        return {
            "id": str(tank.id),
            "name": tank.name,
            "factory": {"id": str(tank.factory_id.id), "name": tank.factory_id.name},
            "tank_type": tank.tank_type,
            "capacity_l": tank.capacity_l,
            "status": tank.status,
            "cleaning_status": tank.cleaning_status,
            "current_product": tank.current_product,
            "current_batch_no": tank.current_batch_no,
            "current_volume_l": tank.current_volume_l,
            "current_temperature": (
                reading.temperature_c if reading else tank.current_temperature
            ),
            "current_pressure": reading.pressure_bar if reading else tank.current_pressure,
            "current_gravity": reading.gravity_plato if reading else tank.current_gravity,
            "session_id": str(session.id) if session else None,
            "session_state": session.state if session else None,
            "open_anomaly_count": len(anomalies),
            "latest_reading_at": (
                reading.recorded_at.isoformat() if reading else None
            ),
        }

    def tanks(self, page, page_size, filters):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self.inventory._allowed_factory_ids()),
            ("active", "=", True),
        ]
        if filters.get("factory_id"):
            self.inventory._factory(filters["factory_id"])
            domain.append(("factory_id", "=", int(filters["factory_id"])))
        if filters.get("status"):
            domain.append(("status", "=", filters["status"]))
        model = self.env["brew.tank"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="factory_id,name",
        )
        return [self._serialize_tank(item) for item in records], total

    def tank_detail(self, tank_id):
        tank = self.env["brew.tank"].sudo().search(
            [
                ("id", "=", int(tank_id or 0)),
                ("company_id", "=", self.company.id),
                ("factory_id", "in", self.inventory._allowed_factory_ids()),
            ],
            limit=1,
        )
        if not tank:
            raise ApiError("RESOURCE_NOT_FOUND", "罐体不存在。", 404)
        data = self._serialize_tank(tank)
        session = self._session_for_tank(tank)
        if session:
            data["session"] = self._serialize_session(session, True)
        return data

    def tank_timeline(self, tank_id):
        detail = self.tank_detail(tank_id)
        session_id = detail.get("session_id")
        return self.session_timeline(session_id) if session_id else []

    def _serialize_session(self, record, detail=False):
        data = {
            "id": str(record.id),
            "name": record.name,
            "state": record.state,
            "batch": {"id": str(record.batch_id.id), "name": record.batch_id.name},
            "factory": {"id": str(record.factory_id.id), "name": record.factory_id.name},
            "tank": {"id": str(record.tank_id.id), "name": record.tank_id.name},
            "source_tank_id": str(record.source_tank_id.id),
            "current_volume_l": record.current_volume_l,
            "started_at": record.started_at.isoformat(),
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at else None
            ),
            "latest_reading": (
                self._serialize_reading(record.latest_reading_id)
                if record.latest_reading_id
                else None
            ),
            "open_anomaly_count": len(
                record.anomaly_ids.filtered(lambda item: item.state != "closed")
            ),
            "version": self._version(record),
        }
        if detail:
            data["nodes"] = [
                {
                    "id": str(node.id),
                    "sequence": node.sequence,
                    "name": node.name,
                    "phase": node.phase,
                    "action_type": node.action_type,
                    "mandatory_confirmation": node.mandatory_confirmation,
                    "state": node.state,
                    "confirmed_by": (
                        node.confirmed_by_id.name if node.confirmed_by_id else None
                    ),
                    "confirmed_at": (
                        node.confirmed_at.isoformat() if node.confirmed_at else None
                    ),
                }
                for node in record.node_ids
            ]
        return data

    def _material_values(self, payload):
        result = []
        for values in payload or []:
            product = self.inventory._product(values.get("product_id"))
            lot = self.inventory._lot(values.get("lot_id"))
            uom = self.env["uom.uom"].sudo().browse(int(values.get("uom_id") or 0)).exists()
            quantity = float(values.get("quantity") or 0)
            if (
                lot.product_id != product
                or not uom
                or uom.category_id != product.uom_id.category_id
                or quantity <= 0
            ):
                raise ApiError("VALIDATION_RELATION", "发酵动作物料、批次、单位或数量无效。", 422)
            result.append(
                {
                    "product_id": product.id,
                    "lot_id": lot.id,
                    "quantity": quantity,
                    "uom_id": uom.id,
                }
            )
        return result

    def add_yeast(self, session_id, payload):
        self._require_operator()
        session = self._record("brew.fermentation.session", session_id)
        self._check_version(session, payload.get("version"))
        materials = self._material_values([payload])
        values = materials[0]
        action = session.add_yeast(
            self.user,
            self.env["product.product"].browse(values["product_id"]),
            self.env["stock.lot"].browse(values["lot_id"]),
            values["quantity"],
            self.env["uom.uom"].browse(values["uom_id"]),
            payload.get("note"),
        )
        return session, self._serialize_session(session, True), str(action.id)

    def perform_action(self, session_id, payload):
        self._require_operator()
        session = self._record("brew.fermentation.session", session_id)
        self._check_version(session, payload.get("version"))
        action = session.perform_action(
            self.user,
            payload.get("action_type"),
            payload.get("parameters") or {},
            self._material_values(payload.get("materials")),
        )
        return session, self._serialize_session(session, True), str(action.id)

    def transfer(self, session_id, payload):
        self._require_operator()
        session = self._record("brew.fermentation.session", session_id)
        self._check_version(session, payload.get("version"))
        target = self.env["brew.tank"].sudo().search(
            [
                ("id", "=", int(payload.get("target_tank_id") or 0)),
                ("company_id", "=", self.company.id),
                ("factory_id", "=", session.factory_id.id),
            ],
            limit=1,
        )
        if not target:
            raise ApiError("VALIDATION_RELATION", "目标罐不存在。", 422)
        session.transfer(
            self.user,
            target,
            payload.get("volume_l"),
            payload.get("confirmed"),
        )
        return session, self._serialize_session(session, True)

    def complete_session(self, session_id, payload):
        self._require_operator()
        session = self._record("brew.fermentation.session", session_id)
        self._check_version(session, payload.get("version"))
        session.action_complete(self.user, payload.get("confirmed"))
        return session, self._serialize_session(session, True)

    def _serialize_reading(self, record):
        return {
            "id": str(record.id),
            "session_id": str(record.session_id.id),
            "recorded_at": record.recorded_at.isoformat(),
            "recorded_by": {
                "id": str(record.recorded_by_id.id),
                "name": record.recorded_by_id.name,
            },
            "temperature_c": record.temperature_c,
            "pressure_bar": record.pressure_bar,
            "gravity_plato": record.gravity_plato,
            "ph": record.ph,
            "volume_l": record.volume_l,
            "state": record.state,
            "is_backfill": record.is_backfill,
            "version_number": record.version,
            "corrects_reading_id": (
                str(record.corrects_reading_id.id)
                if record.corrects_reading_id
                else None
            ),
            "correction_reason": record.correction_reason,
            "note": record.note,
            "return_reason": record.return_reason,
            "version": self._version(record),
        }

    def readings(self, session_id, page, page_size):
        session = self._record("brew.fermentation.session", session_id)
        model = self.env["brew.fermentation.reading"].sudo()
        domain = [("session_id", "=", session.id)]
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="recorded_at desc,id desc",
        )
        return [self._serialize_reading(item) for item in records], total

    def create_reading(self, session_id, payload):
        self._require_operator()
        session = self._record("brew.fermentation.session", session_id)
        if session.state in {"completed", "terminated"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "发酵会话已结束。", 422)
        required = {"temperature_c", "pressure_bar", "gravity_plato"}
        if not required.issubset(payload):
            raise ApiError("VALIDATION_REQUIRED", "温度、压力和糖度不能为空。", 422)
        recorded_at = payload.get("recorded_at") or fields.Datetime.now()
        is_backfill = fields.Datetime.to_datetime(recorded_at).date() < fields.Date.today()
        record = self.env["brew.fermentation.reading"].sudo().create(
            {
                "session_id": session.id,
                "recorded_at": recorded_at,
                "recorded_by_id": self.user.id,
                "temperature_c": float(payload["temperature_c"]),
                "pressure_bar": float(payload["pressure_bar"]),
                "gravity_plato": float(payload["gravity_plato"]),
                "ph": float(payload.get("ph") or 0),
                "volume_l": float(payload.get("volume_l") or 0),
                "is_backfill": is_backfill,
                "note": payload.get("note"),
                "attachment_ids": [
                    Command.set([int(item) for item in payload.get("attachment_ids") or []])
                ],
            }
        )
        return record, self._serialize_reading(record)

    def reading_action(self, reading_id, action, payload):
        reading = self._record("brew.fermentation.reading", reading_id)
        self._check_version(reading, payload.get("version"))
        if action == "submit":
            self._require_operator()
            reading.action_submit()
        elif action == "approve":
            self._require_director()
            reading.action_approve(self.user)
        elif action == "correct":
            self._require_director()
            correction = reading.create_correction(payload.get("reason"))
            return correction, self._serialize_reading(correction)
        elif action == "return":
            self._require_director()
            reading.action_return(payload.get("reason"))
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "发酵读数动作不存在。", 404)
        return reading, self._serialize_reading(reading)

    def trends(self, session_id):
        session = self._record("brew.fermentation.session", session_id)
        records = session.reading_ids.filtered(
            lambda item: item.state == "approved"
        ).sorted("recorded_at")
        return {
            "session_id": str(session.id),
            "series": [
                {
                    "at": item.recorded_at.isoformat(),
                    "temperature_c": item.temperature_c,
                    "pressure_bar": item.pressure_bar,
                    "gravity_plato": item.gravity_plato,
                    "ph": item.ph,
                    "volume_l": item.volume_l,
                }
                for item in records
            ],
        }

    def session_timeline(self, session_id):
        session = self._record("brew.fermentation.session", session_id)
        items = [
            {
                "at": session.started_at.isoformat(),
                "type": "session_started",
                "title": "发酵会话开始",
            }
        ]
        items.extend(
            {
                "at": item.recorded_at.isoformat(),
                "type": "reading",
                "title": "发酵读数",
                "id": str(item.id),
                "state": item.state,
            }
            for item in session.reading_ids
        )
        items.extend(
            {
                "at": item.performed_at.isoformat(),
                "type": f"action.{item.action_type}",
                "title": item.action_type,
                "id": str(item.id),
            }
            for item in session.action_ids
        )
        items.extend(
            {
                "at": item.detected_at.isoformat(),
                "type": f"anomaly.{item.anomaly_type}",
                "title": item.name,
                "id": str(item.id),
                "state": item.state,
            }
            for item in session.anomaly_ids
        )
        return sorted(items, key=lambda item: item["at"])

    def _serialize_anomaly(self, record):
        return {
            "id": str(record.id),
            "name": record.name,
            "session": {"id": str(record.session_id.id), "name": record.session_id.name},
            "tank": {
                "id": str(record.session_id.tank_id.id),
                "name": record.session_id.tank_id.name,
            },
            "anomaly_type": record.anomaly_type,
            "severity": record.severity,
            "state": record.state,
            "detected_at": record.detected_at.isoformat(),
            "last_detected_at": record.last_detected_at.isoformat(),
            "occurrence_count": record.occurrence_count,
            "metrics": json.loads(record.metrics_json or "{}"),
            "assignee": (
                {"id": str(record.assignee_id.id), "name": record.assignee_id.name}
                if record.assignee_id
                else None
            ),
            "resolution": record.resolution,
            "version": self._version(record),
        }

    def anomalies(self, page, page_size, filters):
        domain = [
            ("company_id", "=", self.company.id),
            ("factory_id", "in", self.inventory._allowed_factory_ids()),
        ]
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("severity"):
            domain.append(("severity", "=", filters["severity"]))
        model = self.env["brew.production.anomaly"].sudo()
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="severity desc,detected_at desc,id desc",
        )
        return [self._serialize_anomaly(item) for item in records], total

    def anomaly_action(self, anomaly_id, action, payload):
        anomaly = self._record("brew.production.anomaly", anomaly_id)
        self._check_version(anomaly, payload.get("version"))
        if action == "assign":
            self._require_director()
            user = self.env["res.users"].sudo().search(
                [
                    ("id", "=", int(payload.get("assignee_id") or 0)),
                    ("company_ids", "in", self.company.id),
                    ("active", "=", True),
                ],
                limit=1,
            )
            if not user:
                raise ApiError("VALIDATION_RELATION", "异常处理人无效。", 422)
            anomaly.action_assign(user)
        elif action == "resolve":
            self._require_operator()
            anomaly.action_resolve(self.user, payload.get("resolution"))
        elif action == "verify":
            self._require_director()
            anomaly.action_verify(self.user)
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "异常动作不存在。", 404)
        return anomaly, self._serialize_anomaly(anomaly)
