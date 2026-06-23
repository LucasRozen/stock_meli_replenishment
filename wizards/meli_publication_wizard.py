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
    is_kit_product = fields.Boolean(
        'Es kit',
        compute='_compute_is_kit_product',
    )
    kit_line_ids = fields.One2many(
        'meli.publication.wizard.kit.line', 'wizard_id',
        string='Componentes del kit',
    )
    available_location_ids = fields.Many2many(
        'stock.location',
        compute='_compute_available_location_ids',
        help='Sub-ubicaciones R/S con stock disponible del producto elegido.',
    )
    source_location_id = fields.Many2one(
        'stock.location',
        string='Ubicación de origen (R/S)',
        domain="[('id', 'in', available_location_ids)]",
        help='Sub-ubicación R/S desde la que se transferirán las unidades. '
             'Si se deja vacío, se toma automáticamente de la(s) ubicación(es) '
             'con más stock.',
    )
    total_available_qty = fields.Float(
        'Total disponible en R/S',
        compute='_compute_total_available_qty',
        readonly=True,
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
    percent_replenish = fields.Float(
        'Porcentaje a reponer (%)',
        default=50.0,
        help='Porcentaje del stock disponible en R/S que se transferirá a '
             'MELI/Stock cuando se ejecute la regla de reabastecimiento.',
    )
    min_stock = fields.Float(
        'Stock mínimo en MELI',
        default=1.0,
        help='Stock mínimo de las reglas que se creen al publicar. Se repone '
             'cuando el stock en MELI/Stock sea MENOR a este valor '
             '(estrictamente). Con el default 1, repone al llegar a 0; con 0 '
             'no repone nunca.',
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

    def _rs_available_qty(self, product, rs_loc):
        """Unidades disponibles (sin reservar) de un producto en R/S."""
        quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', rs_loc.id),
        ])
        return sum(max(0.0, q.quantity - q.reserved_quantity) for q in quants)

    @api.depends('product_id')
    def _compute_is_kit_product(self):
        for w in self:
            w.is_kit_product = bool(
                w.product_id and w.product_id._meli_is_kit())

    @api.depends('product_id')
    def _compute_available_location_ids(self):
        rule_model = self.env['meli.replenishment.rule']
        rs_loc = rule_model._get_rs_location()
        for w in self:
            # Para kits no aplica elegir una sola ubicación (cada componente
            # tiene las suyas); se resuelve automáticamente por componente.
            if not w.product_id or not rs_loc or w.product_id._meli_is_kit():
                w.available_location_ids = False
                continue
            quants = self.env['stock.quant'].search([
                ('product_id', '=', w.product_id.id),
                ('location_id', 'child_of', rs_loc.id),
                ('quantity', '>', 0),
            ])
            locs = quants.filtered(
                lambda q: q.quantity - q.reserved_quantity > 0
            ).mapped('location_id')
            w.available_location_ids = locs

    @api.onchange('product_id')
    def _onchange_product_id(self):
        # Al cambiar de producto, limpiar la ubicación elegida (puede no aplicar)
        self.source_location_id = False
        if self.product_id:
            self.price_usd = self.product_id.product_tmpl_id.price_usd or 0.0
        else:
            self.price_usd = 0.0

        # Poblar/limpiar las líneas de componentes según sea kit o no.
        if self.product_id and self.product_id._meli_is_kit():
            comps = self.product_id._meli_kit_components()
            self.kit_line_ids = [(5, 0, 0)] + [
                (0, 0, {
                    'product_id': component.id,
                    'qty_per_kit': qty_per_kit,
                })
                for component, qty_per_kit in comps
            ]
        else:
            self.kit_line_ids = [(5, 0, 0)]

    @api.depends('product_id')
    def _compute_total_available_qty(self):
        rule_model = self.env['meli.replenishment.rule']
        rs_loc = rule_model._get_rs_location()
        for w in self:
            if not w.product_id or not rs_loc:
                w.total_available_qty = 0.0
                continue
            if w.product_id._meli_is_kit():
                # Para un kit, "disponible" = cuántos kits se pueden armar con
                # el stock de componentes en R/S (mínimo entre componentes).
                comps = w.product_id._meli_kit_components()
                buildable = []
                for component, qty_per_kit in comps:
                    if qty_per_kit <= 0:
                        continue
                    avail = w._rs_available_qty(component, rs_loc)
                    buildable.append(int(avail // qty_per_kit))
                w.total_available_qty = min(buildable) if buildable else 0.0
            else:
                w.total_available_qty = w._rs_available_qty(
                    w.product_id, rs_loc)

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

        meli_replenishment = self.env['meli.replenishment.rule']
        rs_loc = meli_replenishment._get_rs_location()
        if not rs_loc:
            raise UserError(_('No se encontró la ubicación R/S configurada.'))

        if self.product_id._meli_is_kit():
            picking, extra_msg = self._confirm_kit(rs_loc)
        else:
            picking, extra_msg = self._confirm_single(rs_loc)

        self.picking_id = picking.id

        # Validar el traslado en el acto (no esperar al cron). Si
        # despacho_a_plaza aún no sincronizó numero.despacho, el helper deja el
        # picking en 'assigned' sin romper y el cron lo reintenta.
        validado = meli_replenishment._validate_replenishment_picking(picking)

        # Guardar Precio MELI (list_price = Precio de venta) en el producto.
        # En este Odoo list_price ya incluye IVA (verificado contra
        # publicaciones existentes), así que se carga el precio_final tal cual.
        self.product_id.product_tmpl_id.list_price = self.precio_final

        estado = (
            _('Transferencia %s validada (Hecho).') % picking.name
            if validado else
            _('Transferencia %s creada (queda en Listo; se validará '
              'automáticamente en breve).') % picking.name
        )
        message = _(
            '%s\nPrecio MELI guardado en producto: $%.2f'
        ) % (estado, self.precio_final)
        if extra_msg:
            message += '\n' + extra_msg

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Publicación MELI creada'),
                'message': message,
                'type': 'success',
                'sticky': bool(extra_msg),
            },
        }

    def _confirm_single(self, rs_loc):
        """Flujo de publicación para un producto individual (no kit).
        Devuelve (picking, mensaje_extra)."""
        meli_replenishment = self.env['meli.replenishment.rule']

        # Si el usuario eligió una ubicación, validar stock sólo en ella.
        src_domain = [
            ('product_id', '=', self.product_id.id),
            ('quantity', '>', 0),
        ]
        if self.source_location_id:
            src_domain.append(
                ('location_id', 'child_of', self.source_location_id.id))
            sin_stock_loc = self.source_location_id.complete_name
        else:
            src_domain.append(('location_id', 'child_of', rs_loc.id))
            sin_stock_loc = rs_loc.complete_name

        rs_quants = self.env['stock.quant'].search(src_domain)
        if not rs_quants:
            raise UserError(
                _('Sin stock disponible en %s para %s.')
                % (sin_stock_loc, self.product_id.display_name)
            )

        available_qty = sum(
            max(0.0, q.quantity - q.reserved_quantity) for q in rs_quants
        )
        if self.qty_to_transfer > available_qty:
            raise UserError(
                _('No hay suficientes unidades disponibles en %s. '
                  'Disponibles: %.0f, Solicitadas: %.0f.')
                % (sin_stock_loc, available_qty, self.qty_to_transfer)
            )

        # Evitar duplicar transferencias pendientes hacia MELI.
        meli_loc = meli_replenishment._get_meli_location()
        if meli_loc:
            pending = self.env['stock.move'].search([
                ('product_id', '=', self.product_id.id),
                ('location_dest_id', 'child_of', meli_loc.id),
                ('state', 'not in', ['done', 'cancel']),
            ], limit=1)
            if pending:
                raise UserError(
                    _('Ya existe una transferencia pendiente hacia MELI para '
                      '%s (picking %s).')
                    % (self.product_id.display_name, pending.picking_id.name)
                )

        picking = meli_replenishment._create_replenishment_picking(
            self.product_id,
            self.qty_to_transfer,
            forced_src_location=self.source_location_id or None,
        )
        if not picking:
            raise UserError(
                _('No se pudo crear la transferencia. Intenta de nuevo.'))

        # Crear o actualizar regla de reabastecimiento del producto.
        if self.crear_regla_reabastecimiento:
            rule_vals = {
                'percent_replenish': self.percent_replenish,
                'min_stock': self.min_stock,
            }
            if self.source_location_id:
                rule_vals['source_location_id'] = self.source_location_id.id
            rule = meli_replenishment.search([
                ('product_id', '=', self.product_id.id),
            ])
            if rule:
                rule.write(rule_vals)
            else:
                rule_vals['product_id'] = self.product_id.id
                meli_replenishment.create(rule_vals)

        return picking, ''

    def _confirm_kit(self, rs_loc):
        """Flujo de publicación para un kit: explota el kit en componentes,
        transfiere cada uno a MELI (desde la ubicación elegida por línea) y
        crea reglas por componente. Devuelve (picking, mensaje_extra)."""
        meli_replenishment = self.env['meli.replenishment.rule']

        # Los componentes se derivan SIEMPRE de la BoM (fuente autoritativa).
        # No se confía en kit_line_ids para la identidad del componente: sus
        # campos product_id/qty_per_kit son readonly y el cliente web no los
        # reenvía al guardar, por lo que las líneas pueden llegar sin producto.
        components = self.product_id._meli_kit_components()
        if not components:
            raise UserError(
                _('El kit %s no tiene componentes en su lista de materiales.')
                % self.product_id.display_name
            )

        # Ubicación de origen elegida por componente (desde las líneas del
        # wizard). Si product_id no sobrevivió al guardado, queda vacío y se
        # usa selección automática.
        src_by_product = {
            line.product_id.id: line.source_location_id
            for line in self.kit_line_ids
            if line.product_id and line.source_location_id
        }

        # needed: [(componente, qty_total, ubicacion_origen_forzada|None), ...]
        needed = [
            (component,
             self.qty_to_transfer * qty_per_kit,
             src_by_product.get(component.id) or None)
            for component, qty_per_kit in components
        ]

        # Validar que cada componente tenga stock suficiente en R/S.
        faltantes = []
        for component, qty, _src in needed:
            avail = self._rs_available_qty(component, rs_loc)
            if avail < qty:
                faltantes.append((component, qty, avail))
        if faltantes:
            detalle = '\n'.join(
                _('  - %s: necesita %.0f, disponible %.0f')
                % (c.display_name, qty, avail)
                for c, qty, avail in faltantes
            )
            raise UserError(
                _('No hay stock suficiente en R/S para armar %.0f kit(s) de '
                  '%s:\n%s')
                % (self.qty_to_transfer, self.product_id.display_name, detalle)
            )

        picking, _f = meli_replenishment._create_kit_replenishment_picking(
            needed,
        )
        if not picking:
            raise UserError(
                _('No se pudo crear la transferencia de los componentes.'))

        # Crear reglas de reabastecimiento por componente (las que falten).
        creadas = []
        if self.crear_regla_reabastecimiento:
            for component, _qty, _src in needed:
                existing = meli_replenishment.search([
                    ('product_id', '=', component.id),
                ], limit=1)
                if existing:
                    continue
                meli_replenishment.create({
                    'product_id': component.id,
                    'percent_replenish': self.percent_replenish,
                    'min_stock': self.min_stock,
                })
                creadas.append(component.display_name)

        extra_msg = ''
        if creadas:
            extra_msg = _('Reglas creadas para: %s') % ', '.join(creadas)
        elif self.crear_regla_reabastecimiento:
            extra_msg = _('Los componentes ya tenían regla de '
                          'reabastecimiento.')
        return picking, extra_msg


class MeliPublicationWizardKitLine(models.TransientModel):
    _name = 'meli.publication.wizard.kit.line'
    _description = 'Componente de kit en wizard de publicación MELI'

    wizard_id = fields.Many2one(
        'meli.publication.wizard', required=True, ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product', string='Componente', readonly=True,
    )
    qty_per_kit = fields.Float('Cant. por kit', readonly=True)
    qty_needed = fields.Float(
        'Necesita', compute='_compute_qty_needed',
    )
    available_location_ids = fields.Many2many(
        'stock.location', compute='_compute_available_location_ids',
    )
    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación de origen (R/S)',
        domain="[('id', 'in', available_location_ids)]",
        help='Sub-ubicación R/S desde la que se tomará este componente. '
             'Si se deja vacío, se toma automáticamente de la(s) que tengan '
             'más stock.',
    )
    available_qty = fields.Float(
        'Disponible en R/S', compute='_compute_available_qty',
    )

    @api.depends('wizard_id.qty_to_transfer', 'qty_per_kit')
    def _compute_qty_needed(self):
        for line in self:
            line.qty_needed = line.wizard_id.qty_to_transfer * line.qty_per_kit

    @api.depends('product_id')
    def _compute_available_location_ids(self):
        rs_loc = self.env['meli.replenishment.rule']._get_rs_location()
        for line in self:
            if not line.product_id or not rs_loc:
                line.available_location_ids = False
                continue
            quants = self.env['stock.quant'].search([
                ('product_id', '=', line.product_id.id),
                ('location_id', 'child_of', rs_loc.id),
                ('quantity', '>', 0),
            ])
            line.available_location_ids = quants.filtered(
                lambda q: q.quantity - q.reserved_quantity > 0
            ).mapped('location_id')

    @api.depends('product_id', 'source_location_id')
    def _compute_available_qty(self):
        rs_loc = self.env['meli.replenishment.rule']._get_rs_location()
        for line in self:
            if not line.product_id or not rs_loc:
                line.available_qty = 0.0
                continue
            loc = line.source_location_id or rs_loc
            quants = self.env['stock.quant'].search([
                ('product_id', '=', line.product_id.id),
                ('location_id', 'child_of', loc.id),
            ])
            line.available_qty = sum(
                max(0.0, q.quantity - q.reserved_quantity) for q in quants
            )
