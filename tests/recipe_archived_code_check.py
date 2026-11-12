"""Run in Odoo shell; verify draft creation against archived codes, then roll back."""
import uuid

from odoo.addons.craft_brew_api.controllers.base import ApiError
from odoo.addons.craft_brew_api.services.recipe import RecipeService


def run(env):
    source = env['product.template'].search([
        ('company_id', '=', env.company.id), ('brew_item_type', '=', 'finished_product'),
        ('brew_beer_category_id', '!=', False), ('brew_beer_subcategory_id', '!=', False),
    ], limit=1)
    equipment = env['maintenance.equipment'].search([
        ('company_id', '=', env.company.id), ('brew_equipment_type', '=', 'mash'),
        ('brew_capacity_l', '>=', 20),
    ], limit=1)
    assert source and equipment, 'A categorized beer product and brewing system are required.'
    product = source.copy({'name': 'CODECHECK-' + uuid.uuid4().hex[:8]})
    service = RecipeService(env, env.ref('base.user_admin'), product.company_id)
    design = {
        'schema_version': 2, 'brew_equipment_id': equipment.id, 'brew_volume_l': 20,
        'ibu_method': 'tinseth', 'fermentables': [], 'hops': [], 'yeast': [],
        'mash': {'water_calculation': 'original', 'efficiency': 72, 'water_grain_ratio': 3,
                 'absorption_rate': .8, 'boil_time_min': 90, 'evaporation_rate': 10,
                 'screen_water_l': 0, 'sparge_count': 1, 'sparge_water_liters': [22],
                 'steps': [], 'workflow': {'execution_mode': 'standard'}},
        'fermentation': {'steps': [{'condition': 'time', 'schedule_type': 'day', 'day_value': 3,
                                    'action': 'measure_gravity', 'target': ''}]},
    }
    payload = {'product_tmpl_id': product.id, 'equipment_id': equipment.id,
               'initial_version': {'base_volume_l': 20, 'design': design}}
    first, _ = service.create_recipe(payload)
    first.active = False
    second, data = service.create_recipe(payload)
    assert first.code.endswith('-1') and second.code.endswith('-2')
    assert data['initial_version']['state'] == 'draft'
    assert first.exists() and not first.active
    for existing in (first, second):
        try:
            service.create_recipe({**payload, 'code': existing.code})
        except ApiError as exc:
            assert exc.code == 'VALIDATION_DUPLICATE' and exc.status == 409
        else:
            raise AssertionError('Duplicate recipe code was accepted')
    print('PASS: draft creation skips archived recipe codes; explicit duplicate codes return 409.')


try:
    run(env)
finally:
    env.cr.rollback()
