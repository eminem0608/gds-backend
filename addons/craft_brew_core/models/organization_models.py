from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError
from psycopg2 import IntegrityError

from ..services.role_service import (
    MENU_DEFINITIONS,
    ROLE_DEFINITIONS,
    SYSTEM_ADMIN_DEFAULT_ROLE_CODES,
    default_page_for_roles,
    menu_codes_for_role,
    permissions_for_roles,
)


class BrewOrganization(models.Model):
    _name = "brew.organization"
    _description = "Craft Brew Organization"
    _order = "sequence, name, id"
    _parent_name = "parent_id"
    _parent_store = True

    display_name = fields.Char(compute="_compute_display_name", recursive=True)
    name = fields.Char(required=True, index=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.company,
    )
    parent_id = fields.Many2one(
        "brew.organization",
        index=True,
        ondelete="restrict",
        domain="[('company_id', '=', company_id)]",
    )
    parent_path = fields.Char(index=True, unaccent=False)
    child_ids = fields.One2many("brew.organization", "parent_id")
    organization_type = fields.Selection(
        [
            ("company", "总部"),
            ("factory", "厂区"),
            ("department", "部门"),
            ("workshop", "车间"),
            ("team", "班组"),
        ],
        required=True,
        default="department",
        index=True,
    )
    address = fields.Char()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        (
            "brew_organization_code_company_unique",
            "unique(company_id, code)",
            "同一企业内组织编码不能重复。",
        )
    ]

    @api.constrains("parent_id", "company_id")
    def _check_parent_company(self):
        if not self._check_recursion():
            raise ValidationError("组织机构不能形成循环层级。")
        for organization in self:
            if organization.parent_id and organization.parent_id.company_id != organization.company_id:
                raise ValidationError("上级组织必须属于同一企业。")

    def write(self, values):
        result = super().write(values)
        if "parent_id" in values:
            users = self.env["res.users"].sudo().search(
                [("brew_organization_id", "child_of", self.ids)]
            )
            users._sync_brew_security_groups()
        return result

    @api.depends("name", "parent_id.display_name")
    def _compute_display_name(self):
        for organization in self:
            organization.display_name = (
                "%s / %s" % (organization.parent_id.display_name, organization.name)
                if organization.parent_id
                else organization.name
            )


class BrewRoleConfiguration(models.Model):
    _name = "brew.role.configuration"
    _description = "Craft Brew Role Configuration"
    _order = "sequence, id"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.company,
    )
    code = fields.Selection(
        selection=lambda self: [
            (code, item["label"]) for code, item in ROLE_DEFINITIONS.items()
        ],
        required=True,
        index=True,
    )
    name = fields.Char(required=True)
    description = fields.Char()
    active = fields.Boolean(default=True, index=True)
    sequence = fields.Integer(default=10)
    menu_codes = fields.Json(string="菜单编码")
    organization_ids = fields.Many2many(
        "brew.organization",
        "brew_role_configuration_organization_rel",
        "role_configuration_id",
        "organization_id",
        string="授权组织",
        domain="[('company_id', '=', company_id)]",
    )

    _sql_constraints = [
        (
            "company_code_unique",
            "unique(company_id, code)",
            "同一企业内角色编码不能重复。",
        )
    ]

    @api.model
    def ensure_company_roles(self, company):
        existing = {
            item.code: item
            for item in self.sudo().with_context(active_test=False).search(
                [("company_id", "=", company.id)]
            )
        }
        for sequence, (code, definition) in enumerate(ROLE_DEFINITIONS.items(), start=1):
            if code not in existing:
                try:
                    with self.env.cr.savepoint():
                        existing[code] = self.sudo().create(
                            {
                                "company_id": company.id,
                                "code": code,
                                "name": definition["label"],
                                "description": "系统内置权限模板",
                                "sequence": sequence * 10,
                                "menu_codes": menu_codes_for_role(code),
                            }
                        )
                except IntegrityError:
                    # Concurrent first page loads can seed the same role at once.
                    existing[code] = self.sudo().with_context(active_test=False).search(
                        [("company_id", "=", company.id), ("code", "=", code)], limit=1
                    )
                    if not existing[code]:
                        raise
        return self.sudo().with_context(active_test=False).search(
            [("company_id", "=", company.id)], order="sequence,id"
        )

    @api.constrains("menu_codes")
    def _check_menu_codes(self):
        valid_codes = {code for code, _label, _roles in MENU_DEFINITIONS}
        for role in self:
            invalid = set(role.menu_codes or []) - valid_codes
            if invalid:
                raise ValidationError("存在未知菜单编码：%s" % ", ".join(sorted(invalid)))

    @api.constrains("organization_ids", "company_id")
    def _check_organization_company(self):
        for role in self:
            if role.organization_ids.filtered(
                lambda item: item.company_id != role.company_id
            ):
                raise ValidationError("角色只能关联当前企业的组织机构。")

    def write(self, values):
        result = super().write(values)
        if {"active", "organization_ids"}.intersection(values):
            system_user_ids = [
                user.id
                for xml_id in (
                    "base.user_root",
                    "base.default_user",
                    "base.public_user",
                    "base.template_portal_user_id",
                )
                if (user := self.env.ref(xml_id, raise_if_not_found=False))
            ]
            users = self.env["res.users"].sudo().search(
                [
                    ("company_ids", "in", self.mapped("company_id").ids),
                    ("id", "not in", system_user_ids),
                ]
            )
            users._sync_brew_security_groups()
        return result


class ResUsers(models.Model):
    _inherit = "res.users"

    brew_organization_id = fields.Many2one(
        "brew.organization",
        string="所属组织",
        index=True,
        ondelete="restrict",
        domain="[('company_id', 'in', company_ids)]",
    )
    brew_job_title = fields.Char(string="岗位")
    brew_is_organization_manager = fields.Boolean(string="组织负责人")
    brew_direct_role_codes = fields.Json(string="直接角色")

    def write(self, values):
        result = super().write(values)
        if not self.env.context.get("skip_brew_role_sync") and {
            "brew_organization_id",
            "brew_is_organization_manager",
            "brew_direct_role_codes",
        }.intersection(values):
            self._sync_brew_security_groups()
        return result

    def _legacy_group_role_codes(self):
        self.ensure_one()
        group_ids = set(self.groups_id.ids)
        return [
            code
            for code, definition in ROLE_DEFINITIONS.items()
            if self.env.ref(definition["xml_id"]).id in group_ids
        ]

    def brew_direct_role_code_list(self):
        self.ensure_one()
        values = self.brew_direct_role_codes
        if not isinstance(values, list):
            codes = self._legacy_group_role_codes()
        else:
            codes = [code for code in values if code in ROLE_DEFINITIONS]
        if not codes and self.has_group("base.group_system"):
            return list(SYSTEM_ADMIN_DEFAULT_ROLE_CODES)
        return codes

    def _organization_ancestors(self):
        self.ensure_one()
        result = self.env["brew.organization"]
        organization = self.brew_organization_id
        while organization:
            result |= organization
            organization = organization.parent_id
        return result

    def _active_role_configurations(self):
        self.ensure_one()
        return self.env["brew.role.configuration"].sudo().search(
            [("company_id", "=", self.company_id.id), ("active", "=", True)]
        )

    def brew_role_codes(self):
        self.ensure_one()
        direct_codes = set(self.brew_direct_role_code_list())
        configurations = self._active_role_configurations()
        configured_codes = set(configurations.mapped("code"))
        effective = {code for code in direct_codes if code not in configured_codes}
        effective.update(
            configurations.filtered(lambda item: item.code in direct_codes).mapped("code")
        )
        ancestors = self._organization_ancestors()
        if ancestors:
            effective.update(
                configurations.filtered(
                    lambda item: bool(item.organization_ids & ancestors)
                ).mapped("code")
            )
        if (
            self.brew_is_organization_manager
            and self.brew_organization_id
            and self.brew_organization_id.organization_type == "factory"
        ):
            effective.add("brewery_manager")
        return [code for code in ROLE_DEFINITIONS if code in effective]

    def brew_role_payloads(self):
        self.ensure_one()
        codes = self.brew_role_codes()
        configurations = {
            item.code: item
            for item in self.env["brew.role.configuration"].sudo().search(
                [("company_id", "=", self.company_id.id), ("code", "in", codes)]
            )
        }
        return [
            {
                "code": code,
                "label": configurations[code].name
                if code in configurations
                else ROLE_DEFINITIONS[code]["label"],
            }
            for code in codes
        ]

    def brew_menu_payloads(self):
        self.ensure_one()
        codes = self.brew_role_codes()
        configurations = {
            item.code: item
            for item in self.env["brew.role.configuration"].sudo().search(
                [("company_id", "=", self.company_id.id), ("code", "in", codes)]
            )
        }
        allowed = set()
        for code in codes:
            configuration = configurations.get(code)
            allowed.update(
                configuration.menu_codes
                if configuration and configuration.menu_codes is not None
                else menu_codes_for_role(code)
            )
        return [
            {"code": code, "label": label}
            for code, label, _roles in MENU_DEFINITIONS
            if code in allowed
        ]

    def _sync_brew_security_groups(self):
        all_role_group_ids = {
            self.env.ref(definition["xml_id"]).id
            for definition in ROLE_DEFINITIONS.values()
        }
        internal_group_id = self.env.ref("base.group_user").id
        for user in self.sudo():
            if not isinstance(user.brew_direct_role_codes, list):
                direct_codes = user._legacy_group_role_codes()
                user.with_context(skip_brew_role_sync=True).write(
                    {"brew_direct_role_codes": direct_codes}
                )
            effective_group_ids = {
                self.env.ref(ROLE_DEFINITIONS[code]["xml_id"]).id
                for code in user.brew_role_codes()
            }
            retained_ids = set(user.groups_id.ids) - all_role_group_ids
            if effective_group_ids:
                retained_ids.add(internal_group_id)
            target_ids = sorted(retained_ids | effective_group_ids)
            if set(user.groups_id.ids) != set(target_ids):
                user.with_context(skip_brew_role_sync=True).write(
                    {"groups_id": [Command.set(target_ids)]}
                )
        return True

    def brew_context_payload(self):
        self.ensure_one()
        payload = super().brew_context_payload()
        role_codes = self.brew_role_codes()
        payload.update(
            {
                "roles": self.brew_role_payloads(),
                "permissions": permissions_for_roles(role_codes),
                "menus": self.brew_menu_payloads(),
                "default_page": default_page_for_roles(role_codes),
                "organization": (
                    {
                        "id": str(self.brew_organization_id.id),
                        "name": self.brew_organization_id.display_name,
                    }
                    if self.brew_organization_id
                    else None
                ),
            }
        )
        return payload
