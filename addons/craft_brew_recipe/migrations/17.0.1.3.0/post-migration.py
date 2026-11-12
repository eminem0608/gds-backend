from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    legacy_versions = env["brew.recipe.version"].search([("state", "=", "approved")])
    for recipe in legacy_versions.mapped("recipe_id"):
        candidates = recipe.version_ids.filtered(
            lambda item: item.state in {"approved", "active"}
        ).sorted(key=lambda item: (item.version, item.id), reverse=True)
        candidates[1:].with_context(brew_state_transition=True).write({"state": "inactive"})
        candidates[:1].with_context(brew_state_transition=True).write({"state": "active"})
