"""Close business documents without erasing physical stock or execution evidence."""
from odoo import fields
from ..controllers.base import ApiError


def can_close(user):
    return any(user.has_group('craft_brew_core.' + group) for group in (
        'group_brew_production_director', 'group_brew_general_manager', 'group_brew_admin'))


def require_close(user):
    if not can_close(user):
        raise ApiError('PERMISSION_DENIED', '只有生产总监、总经理或系统管理员可以取消、删除已排产单据。', 403)


def close_plan(env, plan, user, reason, delete=False):
    """Caller holds the plan row lock. Posted movements and in-tank liquid survive."""
    require_close(user)
    if plan.deleted_at:
        raise ApiError('STATE_PRECONDITION_FAILED', '生产计划已删除。', 409)
    if not delete and plan.state in {'cancelled', 'terminated'}:
        raise ApiError('STATE_PRECONDITION_FAILED', '生产计划已经关闭。', 409)
    now = fields.Datetime.now()
    reservations = env['brew.inventory.reservation'].sudo().search([
        ('company_id', '=', plan.company_id.id), ('source_model', '=', plan._name),
        ('source_id', '=', plan.id), ('state', '=', 'active')])
    requisitions = env['brew.material.requisition'].sudo().search([
        ('company_id', '=', plan.company_id.id), ('production_plan_id', '=', plan.id)])
    reservations |= env['brew.inventory.reservation'].sudo().search([
        ('company_id', '=', plan.company_id.id), ('source_model', '=', 'brew.material.requisition'),
        ('source_id', 'in', requisitions.ids), ('state', '=', 'active')])
    reservations.action_release()
    # Issued/acknowledged material remains available for warehouse settlement.
    requisitions.filtered(lambda r:r.state in {'draft', 'recommended', 'overage_pending'}).write({'state':'cancelled'})
    env['brew.task'].sudo().search([
        ('company_id', '=', plan.company_id.id), ('target_model', '=', plan._name),
        ('target_id', '=', plan.id), ('state', 'not in', ['done', 'cancelled'])
    ]).write({'state':'cancelled', 'last_comment':reason})
    env['brew.approval.instance'].sudo().search([
        ('company_id', '=', plan.company_id.id), ('target_model', '=', plan._name),
        ('target_id', '=', plan.id), ('state', '=', 'pending')
    ]).write({'state':'cancelled', 'completed_at':now})
    values = {'cancel_reason':reason, 'closed_at':now, 'closed_by_id':user.id}
    if plan.state not in {'cancelled', 'terminated'}:
        values['state'] = 'terminated' if plan.state in {'in_progress', 'done'} else 'cancelled'
    if delete:
        values.update(deleted_at=now, deleted_by_id=user.id)
    plan.with_context(brew_state_transition=True).write(values)
