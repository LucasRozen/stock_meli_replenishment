from odoo import models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    def name_get(self):
        """Mostrar el stock disponible en la ubicación si se busca para un
        producto específico (vía context).
        """
        result = []
        product_id = self.env.context.get('stock_location_product_id')
        for loc in self:
            name = loc.complete_name
            if product_id:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', product_id),
                    ('location_id', 'child_of', loc.id),
                ])
                available = sum(
                    max(0.0, q.quantity - q.reserved_quantity)
                    for q in quants
                )
                name = f"{name} ({available:.0f} u.)"
            result.append((loc.id, name))
        return result
