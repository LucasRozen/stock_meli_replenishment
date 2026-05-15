import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

IVA_RATE = 1.21


class MeliPublicationWizard(models.TransientModel):
    _name = 'meli.publication.wizard'
    _description = 'Wizard para nueva publicación MELI'

    product_id = fields.Many2one(
        'product.product',
        string='Producto',
        required=True,
        domain=[('type', 'in', ['product', 'consu']), ('list_price', '<=', 1)],
        help='Sólo se muestran productos sin publicación previa (Precio MELI ≤ 1).',
    )
    qty_to_transfer = fields.Float(
        'Unidades a transferir',
        default=10.0,
        required=True,
    )
    price_usd = fields.Float(
        'Precio USD',
        readonly=True,
        help='Se toma del producto. Para modificarlo, editar el producto.',
    )
    dolar_rogrim = fields.Float(
        'Dólar Rogrim',
        required=True,
    )
    precio_neto = fields.Float(
        'Precio neto',
        compute='_compute_precio_neto',
        store=False,
    )
    gasto_fijo = fields.Float(
        'Gasto fijo',
        compute='_compute_gasto_fijo',
        store=False,
    )
    precio_base = fields.Float(
        'Precio base',
        compute='_compute_precio_base',
        store=False,
        help='Precio sugerido: precio neto + gasto fijo (con IVA incluido).',
    )
    precio_final = fields.Float(
        'Precio final',
        required=True,
        help='Editable. Es el precio con IVA que se publica en MELI '
             'y se guarda en el "Precio de venta" del producto.',
    )
    crear_regla_reabastecimiento = fields.Boolean(
        'Crear regla de reabastecimiento automático',
        default=True,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Picking creado',
        readonly=True,
    )

    def _default_dolar_rogrim(self):
        rate = self.env['res.currency.rate'].search(
            [],
            order='name desc',
            limit=1,
        )
        return rate.inverse_company_rate if rate else 0.0

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'dolar_rogrim' in fields_list:
            res['dolar_rogrim'] = self._default_dolar_rogrim()
        return res

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.price_usd = self.product_id.product_tmpl_id.price_usd or 0.0
        else:
            self.price_usd = 0.0

    @api.depends('price_usd', 'dolar_rogrim')
    def _compute_precio_neto(self):
        for w in self:
            w.precio_neto = w.price_usd * IVA_RATE * 1.14 * w.dolar_rogrim

    @api.depends('precio_neto')
    def _compute_gasto_fijo(self):
        for w in self:
            n = w.precio_neto
            if n == 0:
                w.gasto_fijo = 0
            elif n < 15000:
                w.gasto_fijo = 1000
            elif n <= 33000:
                w.gasto_fijo = 2000
            else:  # n > 33000
                w.gasto_fijo = 7500

    @api.depends('precio_neto', 'gasto_fijo')
    def _compute_precio_base(self):
        for w in self:
            w.precio_base = w.precio_neto + w.gasto_fijo

    @api.onchange('precio_base')
    def _onchange_precio_base(self):
        """Cada vez que cambia el precio base (por cambio de producto, USD o
        dólar), sobrescribe el precio final con el nuevo valor sugerido."""
        self.precio_final = self.precio_base

    def action_confirm(self):
        self.ensure_one()

        # Validaciones
        if not self.product_id:
            raise UserError(_('Debes seleccionar un producto.'))
        if self.qty_to_transfer <= 0:
            raise UserError(_('La cantidad a transferir debe ser mayor a 0.'))
        if self.product_id.list_price > 1:
            raise UserError(
                _('El producto %s ya tiene Precio MELI (%s). Ya está publicado.')
                % (self.product_id.display_name, self.product_id.list_price)
            )
        if self.precio_final <= 0:
            raise UserError(_('El precio final debe ser mayor a 0.'))

        # Verificar que hay stock en R/S
        meli_replenishment = self.env['meli.replenishment.rule']
        rs_loc = meli_replenishment._get_rs_location()
        if not rs_loc:
            raise UserError(_('No se encontró la ubicación R/S configurada.'))

        rs_quants = self.env['stock.quant'].search([
            ('product_id', '=', self.product_id.id),
            ('location_id', 'child_of', rs_loc.id),
            ('quantity', '>', 0),
        ])

        if not rs_quants:
            raise UserError(
                _('Sin stock disponible en %s para %s.')
                % (rs_loc.complete_name, self.product_id.display_name)
            )

        # Crear picking
        picking = self.env['meli.replenishment.rule']._create_replenishment_picking(
            self.product_id,
            self.qty_to_transfer,
        )

        if not picking:
            raise UserError(_('No se pudo crear la transferencia. Intenta de nuevo.'))

        self.picking_id = picking.id

        # Guardar Precio MELI (list_price = Precio de venta) en el producto.
        # En este Odoo list_price ya incluye IVA (verificado contra publicaciones
        # existentes), así que se carga el precio_final tal cual.
        self.product_id.product_tmpl_id.list_price = self.precio_final

        # Crear o actualizar regla de reabastecimiento
        if self.crear_regla_reabastecimiento:
            rule = self.env['meli.replenishment.rule'].search([
                ('product_id', '=', self.product_id.id),
            ])
            if rule:
                rule.qty_replenish = self.qty_to_transfer
            else:
                self.env['meli.replenishment.rule'].create({
                    'product_id': self.product_id.id,
                    'qty_replenish': self.qty_to_transfer,
                })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Publicación MELI creada'),
                'message': _(
                    'Transferencia: %s\n'
                    'Precio MELI guardado en producto: $%.2f'
                ) % (picking.name, self.precio_final),
                'type': 'success',
                'sticky': False,
            },
        }
