from odoo import api, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _meli_kit_bom(self):
        """Devuelve la BoM phantom (kit) del producto, o False si no es
        un kit (o si mrp no está instalado)."""
        self.ensure_one()
        if 'mrp.bom' not in self.env:
            return False
        return self.env['mrp.bom'].search([
            ('type', '=', 'phantom'),
            ('product_tmpl_id', '=', self.product_tmpl_id.id),
            '|', ('product_id', '=', self.id), ('product_id', '=', False),
        ], limit=1)

    def _meli_is_kit(self):
        """True si el producto es un kit (tiene BoM phantom)."""
        self.ensure_one()
        return bool(self._meli_kit_bom())

    def _meli_kit_components(self):
        """Explota el kit un nivel y devuelve [(componente, qty_por_kit), ...].
        Lista vacía si no es kit."""
        self.ensure_one()
        bom = self._meli_kit_bom()
        if not bom:
            return []
        return [(line.product_id, line.product_qty)
                for line in bom.bom_line_ids if line.product_id]

    @api.model
    def _meli_kit_product_ids(self):
        """IDs de todos los product.product que son kits (BoM phantom)."""
        if 'mrp.bom' not in self.env:
            return []
        phantom_boms = self.env['mrp.bom'].search([('type', '=', 'phantom')])
        kit_tmpl_ids = phantom_boms.mapped('product_tmpl_id').ids
        if not kit_tmpl_ids:
            return []
        return self.env['product.product'].search([
            ('product_tmpl_id', 'in', kit_tmpl_ids),
        ]).ids

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None,
                     order=None):
        """Excluir productos con regla de reabastecimiento MELI, y los kits,
        cuando se busca desde el formulario de regla (context flag).
        """
        if self.env.context.get('exclude_meli_replenishment_rules'):
            extra = []
            used_ids = self.env['meli.replenishment.rule'].search(
                []
            ).mapped('product_id').ids
            if used_ids:
                extra.append(('id', 'not in', used_ids))
            kit_ids = self._meli_kit_product_ids()
            if kit_ids:
                extra.append(('id', 'not in', kit_ids))
            domain = (domain or []) + extra
        return super()._name_search(
            name, domain=domain, operator=operator,
            limit=limit, order=order,
        )
