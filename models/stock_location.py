from odoo import api, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    @api.depends_context('stock_location_product_id')
    def _compute_display_name(self):
        """Mostrar el stock disponible en la ubicación si se busca para un
        producto específico (vía context). En Odoo 17 name_get fue
        reemplazado por _compute_display_name.
        """
        super()._compute_display_name()
        product_id = self.env.context.get('stock_location_product_id')
        if not product_id:
            return
        for loc in self:
            quants = self.env['stock.quant'].search([
                ('product_id', '=', product_id),
                ('location_id', 'child_of', loc.id),
            ])
            available = sum(
                max(0.0, q.quantity - q.reserved_quantity)
                for q in quants
            )
            loc.display_name = f"{loc.display_name} ({available:.0f} u.)"
