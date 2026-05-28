from odoo import api, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None,
                     order=None):
        """Excluir productos con regla de reabastecimiento MELI cuando se
        busca desde el formulario de regla (context flag).
        """
        if self.env.context.get('exclude_meli_replenishment_rules'):
            used_ids = self.env['meli.replenishment.rule'].search(
                []
            ).mapped('product_id').ids
            extra = [('id', 'not in', used_ids)] if used_ids else []
            domain = (domain or []) + extra
        return super()._name_search(
            name, domain=domain, operator=operator,
            limit=limit, order=order,
        )
