"""Account administration follows organization boundaries, independently of roles."""
from odoo import Command
from ..controllers.base import ApiError


class AccountAccess:
    GLOBAL_ROLES = {'tenant_admin', 'general_manager', 'headquarters_planner', 'supplier'}

    def __init__(self, env, actor, company):
        self.env, self.actor, self.company = env, actor, company
        self.headquarters = actor.brew_has_headquarters_scope(company)
        self.factories = actor.brew_allowed_factories(company, include_archived=True)
        self.administrator = actor.has_group('craft_brew_core.group_brew_admin')
        self.manager = actor.has_group('craft_brew_core.group_brew_brewery_manager')
        self.can_manage = self.administrator or (self.manager and not self.headquarters)
        self.can_configure = self.administrator and self.headquarters

    def require(self, mutation=False, global_only=False):
        reader = self.can_manage or self.actor.has_group('craft_brew_core.group_brew_general_manager')
        if not reader or (mutation and not self.can_manage) or (global_only and not self.can_configure):
            raise ApiError('PERMISSION_DENIED', '当前账号无权执行此账号管理操作。', 403)

    def organizations(self):
        model = self.env['brew.organization'].sudo().with_context(active_test=False)
        domain = [('company_id', '=', self.company.id)]
        if not self.headquarters:
            roots = model.search(domain + [('organization_type', '=', 'factory'), ('factory_id', 'in', self.factories.ids)])
            domain += [('id', 'child_of', roots.ids)]
        return model.search(domain, order='sequence,name,id')

    def home_factory(self, user):
        factory = user.brew_scope_factory(self.company)
        if factory or user.brew_organization_id or user.brew_has_headquarters_scope(self.company):
            return factory
        return user.brew_allowed_factories(self.company, include_archived=True)[:1]

    def visible_users(self, users):
        if self.headquarters:
            return users
        return users.filtered(lambda user: self.home_factory(user) in self.factories)

    def manageable(self, target):
        if not self.can_manage:
            return False
        if self.headquarters:
            return True
        return self.home_factory(target) in self.factories and not bool(set(target.brew_direct_role_code_list()) & self.GLOBAL_ROLES)

    def require_target(self, target):
        if not self.manageable(target):
            raise ApiError('DATA_SCOPE_DENIED', '只能管理本厂区的非总部账号。', 403)

    def validate_roles(self, codes):
        if not self.headquarters and set(codes or []) & self.GLOBAL_ROLES:
            raise ApiError('DATA_SCOPE_DENIED', '厂区管理员不能授予总部或系统管理员角色。', 403)

    def validate_organization(self, organization, parent=None, creating=False, new_type=None):
        if self.can_configure:
            return
        if not self.can_manage or (organization and organization not in self.organizations()):
            raise ApiError('DATA_SCOPE_DENIED', '只能维护本厂区下的部门、车间和班组。', 403)
        if (new_type or (organization.organization_type if organization else None)) not in {'department', 'workshop', 'team'}:
            raise ApiError('DATA_SCOPE_DENIED', '总部和厂区节点由总部管理员维护。', 403)
        if (creating or parent is not None) and (not parent or parent not in self.organizations()):
            raise ApiError('DATA_SCOPE_DENIED', '上级组织必须在本厂区内。', 403)

    def user_values(self, payload, target=None, organization=None):
        if target:
            self.require_target(target)
        organization = organization if organization is not None else (target.brew_organization_id if target else self.env['brew.organization'])
        if organization and organization.company_id != self.company:
            raise ApiError('DATA_SCOPE_DENIED', '组织不属于当前企业。', 403)
        if not self.headquarters and (not organization or organization not in self.organizations()):
            raise ApiError('DATA_SCOPE_DENIED', '账号必须归属于本厂区组织。', 403)
        if 'role_codes' in payload:
            self.validate_roles(payload['role_codes'])
            if target == self.actor and 'tenant_admin' in target.brew_direct_role_code_list() and 'tenant_admin' not in payload['role_codes']:
                raise ApiError('STATE_ACTION_NOT_ALLOWED', '不能移除当前账号的系统管理员角色。', 422)
        factory = self.env['brew.factory']
        node = organization
        while node:
            if node.organization_type == 'factory':
                factory = node.factory_id
                if not factory:
                    raise ApiError('VALIDATION_RELATION', '所属厂区组织尚未关联厂区，请先补齐配置。', 422)
                break
            node = node.parent_id
        values = {}
        if factory:
            requested = {int(item) for item in payload.get('factory_ids', factory.ids)}
            if requested != {factory.id}:
                raise ApiError('DATA_SCOPE_DENIED', '厂区账号的数据范围由组织归属确定，不能授权其他厂区。', 403)
            values.update(brew_factory_ids=[Command.set(factory.ids)], brew_default_factory_id=factory.id)
        elif 'factory_ids' in payload:
            requested = {int(item) for item in payload.get('factory_ids') or []}
            factories = self.env['brew.factory'].sudo().with_context(active_test=False).search([('company_id', '=', self.company.id), ('id', 'in', list(requested))])
            if set(factories.ids) != requested or (not self.headquarters and not factories <= self.factories):
                raise ApiError('DATA_SCOPE_DENIED', '厂区不在当前账号的管理范围内。', 403)
            default = int(payload.get('default_factory_id') or 0)
            if default and default not in requested:
                raise ApiError('VALIDATION_RELATION', '默认厂区必须在授权范围内。', 422)
            retained = target.brew_factory_ids.filtered(lambda f: f.company_id != self.company) if target else self.env['brew.factory']
            values.update(brew_factory_ids=[Command.set((retained | factories).ids)], brew_default_factory_id=default or False)
        return values

    def context(self):
        return {'scope_kind': 'headquarters' if self.headquarters else 'factory',
                'scope_label': '总部 · 全部厂区' if self.headquarters else '、'.join(self.factories.mapped('name')) or '未分配厂区',
                'can_manage_users': self.can_manage, 'can_configure_roles': self.can_configure,
                'can_manage_organizations': self.can_manage,
                'assignable_role_codes': None if self.can_configure else [code for code in self.actor.env['brew.role.configuration'].sudo().search([('company_id', '=', self.company.id)]).mapped('code') if code not in self.GLOBAL_ROLES],
                'factories': [{'id': str(f.id), 'name': f.name, 'active': f.active} for f in self.factories]}
