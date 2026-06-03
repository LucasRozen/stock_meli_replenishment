import logging
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

MELI_STOCK_NAME = 'MELI/Stock'
RS_STOCK_NAME = 'R/S'


class MeliReplenishmentRule(models.Model):
    _name = 'meli.replenishment.rule'
    _description = 'Regla de Reabastecimiento MELI'
    _order = 'product_id'

    product_id = fields.Many2one(
        'product.product', string='Producto', required=True, index=True,
        domain="[('type', 'in', ['product', 'consu']),"
               " ('list_price', '>', 1)]",
        help='Sólo se muestran productos ya publicados en MELI '
             '(Precio MELI > 1) sin regla existente.',
    )
    percent_replenish = fields.Float(
        'Porcentaje a reponer (%)', default=50.0, required=True,
        help='Porcentaje del stock disponible en R/S que se transferirá a '
             'MELI/Stock cuando se ejecute el reabastecimiento.',
    )
    available_location_ids = fields.Many2many(
        'stock.location',
        compute='_compute_available_location_ids',
        help='Sub-ubicaciones R/S con stock disponible del producto elegido.',
    )
    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación de origen (R/S/x)',
        domain="[('id', 'in', available_location_ids)]",
        help='Sub-ubicación R/S desde la que se tomarán las unidades. '
             'Sólo se muestran las que tienen stock del producto. '
             'Si se deja vacío, se toman automáticamente de la(s) ubicación(es) '
             'con más stock.',
    )
    active = fields.Boolean(default=True)
    last_trigger = fields.Datetime('Último reabastecimiento creado', readonly=True)
    last_source_location = fields.Many2one(
        'stock.location', string='Última origen R/S usada', readonly=True,
    )

    _sql_constraints = [
        ('product_unique', 'UNIQUE(product_id)',
         'Ya existe una regla para este producto.'),
        ('percent_replenish_valid',
         'CHECK(percent_replenish > 0 AND percent_replenish <= 100)',
         'El porcentaje debe estar entre 0 y 100.'),
    ]

    @api.depends('product_id')
    def _compute_available_location_ids(self):
        rs_loc = self._get_rs_location()
        for rule in self:
            if not rule.product_id or not rs_loc:
                rule.available_location_ids = False
                continue
            quants = self.env['stock.quant'].search([
                ('product_id', '=', rule.product_id.id),
                ('location_id', 'child_of', rs_loc.id),
                ('quantity', '>', 0),
            ])
            locs = quants.filtered(
                lambda q: q.quantity - q.reserved_quantity > 0
            ).mapped('location_id')
            rule.available_location_ids = locs

    @api.onchange('product_id')
    def _onchange_product_id(self):
        # Al cambiar de producto, limpiar la ubicación elegida (puede no aplicar)
        self.source_location_id = False

    @api.model
    def _get_meli_location(self):
        loc = self.env['stock.location'].search(
            [('complete_name', '=', MELI_STOCK_NAME), ('usage', '=', 'internal')], limit=1,
        )
        if not loc:
            _logger.error('No se encontró la ubicación "%s"', MELI_STOCK_NAME)
        return loc

    @api.model
    def _get_rs_location(self):
        loc = self.env['stock.location'].search(
            [('complete_name', '=', RS_STOCK_NAME), ('usage', '=', 'internal')], limit=1,
        )
        if not loc:
            _logger.error('No se encontró la ubicación "%s"', RS_STOCK_NAME)
        return loc

    @api.model
    def _get_internal_picking_type(self, src_location):
        # Busca el tipo de operación interna cuyo almacén contiene la ubicación origen
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('default_location_src_id', '=', src_location.id),
        ], limit=1)
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('warehouse_id.lot_stock_id', 'child_of', src_location.id),
            ], limit=1)
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search(
                [('code', '=', 'internal')], limit=1,
            )
        return picking_type

    @api.model
    def _get_dest_location(self, src_location, meli_loc, rs_loc):
        # MELI/Stock/<sufijo> donde sufijo = parte después de "R/S/"
        # Ej: R/S/31-D32 → MELI/Stock/31-D32. Si no existe, usa MELI/Stock.
        rs_prefix = rs_loc.complete_name + '/'
        if src_location.complete_name.startswith(rs_prefix):
            subloc_suffix = src_location.complete_name[len(rs_prefix):]
            dest_name = meli_loc.complete_name + '/' + subloc_suffix
            dest_location = self.env['stock.location'].search([
                ('complete_name', '=', dest_name),
                ('usage', '=', 'internal'),
            ], limit=1)
            if dest_location:
                return dest_location
            _logger.warning(
                'No existe %s, usando %s como destino.',
                dest_name, meli_loc.complete_name,
            )
        return meli_loc

    @api.model
    def _create_replenishment_picking(self, product, qty, meli_loc=None,
                                      rs_loc=None, forced_src_location=None):
        """Crea picking R/S → MELI/Stock por la cantidad pedida.
        Si se pasa forced_src_location, prioriza esa sub-ubicación y, si no
        alcanza, completa con las demás sub-ubicaciones de R/S (mayor a menor).
        El destino se calcula automáticamente por sufijo (R/S/x → MELI/Stock/x).
        Devuelve el picking creado, o False si no hay stock disponible en R/S."""
        if not meli_loc:
            meli_loc = self._get_meli_location()
        if not rs_loc:
            rs_loc = self._get_rs_location()

        if not meli_loc or not rs_loc:
            return False

        # 1. Sub-ubicaciones R/S con stock disponible (ordenadas por cantidad desc).
        rs_quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', rs_loc.id),
            ('quantity', '>', 0),
        ], order='quantity desc')

        if not rs_quants:
            _logger.warning(
                'Sin stock en %s para %s, no se puede reabastecer.',
                RS_STOCK_NAME, product.display_name,
            )
            return False

        # Si hay ubicación forzada, ordenar sus quants primero y el resto
        # después (cada grupo por cantidad desc). Así se prioriza la fija y se
        # cae automáticamente a las demás cuando no alcanza.
        if forced_src_location:
            forced_ids = self.env['stock.location'].search([
                ('id', 'child_of', forced_src_location.id),
            ]).ids
            forced = rs_quants.filtered(
                lambda q: q.location_id.id in forced_ids
            )
            rest = rs_quants - forced
            rs_quants = forced + rest

        # 2. Acumular ubicaciones de origen hasta cubrir qty.
        remaining = qty
        transfers = []  # lista de (src_location, qty, dest_location)
        for quant in rs_quants:
            if remaining <= 0:
                break
            available = quant.quantity - quant.reserved_quantity
            if available <= 0:
                continue
            qty_from_this = min(available, remaining)
            dest = self._get_dest_location(
                quant.location_id, meli_loc, rs_loc,
            )
            transfers.append((quant.location_id, qty_from_this, dest))
            remaining -= qty_from_this

        if not transfers:
            _logger.warning(
                'Stock reservado impide el reabastecimiento de %s en %s.',
                product.display_name, RS_STOCK_NAME,
            )
            return False

        total_qty = sum(t[1] for t in transfers)
        picking_type = self._get_internal_picking_type(rs_loc)

        # El picking usa la ubicación padre (R/S) para que Odoo muestre la sub-ubicación
        # específica en la columna "Ubicacion de origen" del tab Operaciones.
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': rs_loc.id,
            'location_dest_id': meli_loc.id,
            'origin': _('Reabastecimiento MELI - %s') % product.display_name,
        })

        move_data = []  # [(move, src, qty, dest), ...]
        for src, qty_move, dest in transfers:
            move = self.env['stock.move'].create({
                'name': product.display_name,
                'product_id': product.id,
                'product_uom_qty': qty_move,
                'product_uom': product.uom_id.id,
                'picking_id': picking.id,
                'location_id': src.id,
                'location_dest_id': dest.id,
                # No seteamos picking_intern_from_location_id: despacho_a_plaza
                # usa el path de move_lines cuando este campo está vacío, que
                # respeta el location_dest_id específico de cada línea
                # (MELI/Stock/xx-YYY) en lugar del location_dest_id del picking
                # padre (MELI/Stock).
            })
            move_data.append((move, src, qty_move, dest))

        picking.action_confirm()
        picking.action_assign()

        # action_confirm/action_assign pueden sobrescribir location_dest_id del
        # move con el del picking (MELI/Stock padre). Forzamos el destino
        # específico (MELI/Stock/x).
        #
        # IMPORTANTE: no borrar las move_lines que generó action_assign(). Esas
        # líneas llevan la reserva del stock en R/S (reserved_quantity del
        # quant). Si se borran, la reserva se libera; y crear una move_line
        # manualmente NO vuelve a reservar (en Odoo 17 el create de move_line
        # sólo toca quants si el move está 'done'). El picking quedaría
        # 'assigned' pero el stock de R/S figuraría disponible, y un despacho a
        # cliente podría reservar las mismas unidades -> R/S en negativo.
        # Por eso sólo corregimos el destino sobre las líneas ya reservadas.
        for move, src, qty_move, dest in move_data:
            if move.location_dest_id != dest:
                move.location_dest_id = dest.id
            if move.move_line_ids:
                move.move_line_ids.write({'location_dest_id': dest.id})
            else:
                # action_assign() no logró reservar (stock tomado en el ínterin
                # por otra operación). Creamos la línea manual como fallback.
                self.env['stock.move.line'].create({
                    'move_id': move.id,
                    'product_id': product.id,
                    'product_uom_id': product.uom_id.id,
                    'quantity': qty_move,
                    'location_id': src.id,
                    'location_dest_id': dest.id,
                    'picking_id': picking.id,
                })

        sources_summary = ', '.join(
            '%s (%.0f u.)' % (src.complete_name, qty_move)
            for src, qty_move, _dest in transfers
        )
        _logger.info(
            'Reabastecimiento creado: %s | %s → MELI | %.0f u. | Picking: %s',
            product.display_name,
            sources_summary,
            total_qty,
            picking.name,
        )
        return picking

    def _run_replenishment_cron(self):
        meli_loc = self._get_meli_location()
        rs_loc = self._get_rs_location()
        if not meli_loc or not rs_loc:
            return

        for rule in self.search([('active', '=', True)]):
            try:
                rule._check_and_replenish(meli_loc, rs_loc)
            except Exception:
                _logger.exception(
                    'Error al procesar regla de reabastecimiento para %s',
                    rule.product_id.display_name,
                )

    def _check_and_replenish(self, meli_loc, rs_loc):
        product = self.product_id

        # 1. Stock real en MELI (todas las sub-ubicaciones, sin contar reservados)
        meli_quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', meli_loc.id),
        ])
        total_meli = sum(q.quantity - q.reserved_quantity for q in meli_quants)

        if total_meli > 0:
            return

        # 2. Evitar duplicados: no crear si ya hay una transferencia pendiente hacia MELI
        pending = self.env['stock.move'].search([
            ('product_id', '=', product.id),
            ('location_dest_id', 'child_of', meli_loc.id),
            ('state', 'not in', ['done', 'cancel']),
        ], limit=1)
        if pending:
            _logger.info(
                'Reabastecimiento pendiente ya existe para %s (picking %s), se omite.',
                product.display_name, pending.picking_id.name,
            )
            return

        # 3. Calcular cantidad a reponer = porcentaje del stock disponible en R/S
        rs_quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', rs_loc.id),
            ('quantity', '>', 0),
        ])
        total_available = sum(
            max(0.0, q.quantity - q.reserved_quantity) for q in rs_quants
        )
        if total_available <= 0:
            _logger.warning(
                'Sin stock disponible en %s para %s (todo reservado).',
                RS_STOCK_NAME, product.display_name,
            )
            return

        qty_to_replenish = int(
            total_available * self.percent_replenish / 100.0
        )
        # Log de diagnóstico: deja registro exacto de lo que vio el cron al
        # calcular, para poder auditar discrepancias de cantidad.
        _logger.info(
            'Cálculo reabastecimiento %s: disponible R/S=%.2f, %% =%.2f, '
            'cantidad calculada=%d',
            product.display_name, total_available, self.percent_replenish,
            qty_to_replenish,
        )
        if qty_to_replenish <= 0:
            _logger.warning(
                'Porcentaje %.2f%% sobre %.0f disponibles da 0 unidades para %s.',
                self.percent_replenish, total_available,
                product.display_name,
            )
            return

        # 4. Delegar creación del picking al helper
        picking = self._create_replenishment_picking(
            product,
            qty_to_replenish,
            meli_loc,
            rs_loc,
            forced_src_location=self.source_location_id or None,
        )

        if not picking:
            return

        self.write({
            'last_trigger': fields.Datetime.now(),
            'last_source_location': picking.move_ids[0].location_id.id if picking.move_ids else False,
        })

    def validate_pending_replenishments(self):
        """Valida pickings de reabastecimiento MELI en estado 'assigned'."""
        pickings = self.env['stock.picking'].search([
            ('origin', 'ilike', 'Reabastecimiento MELI'),
            ('state', '=', 'assigned'),
        ])
        validated = []
        for picking in pickings:
            try:
                result = picking.with_context(
                    skip_backorder=True,
                    is_barcode=True,
                ).button_validate()
                if isinstance(result, dict) and result.get('res_model'):
                    wizard = self.env[result['res_model']].browse(
                        result.get('res_id'))
                    if wizard and hasattr(wizard, 'process'):
                        wizard.process()
                    elif wizard and hasattr(wizard, 'apply'):
                        wizard.apply()
                if picking.state == 'done':
                    validated.append(picking.name)
                    _logger.info('Picking %s validado automaticamente.',
                                 picking.name)
                else:
                    _logger.warning(
                        'Picking %s no quedó en done (state=%s).',
                        picking.name, picking.state)
            except ValidationError as e:
                msg = str(e)
                # despacho_a_plaza lanza ValidationError cuando los
                # numero.despacho aún no están sincronizados con el quant.
                # Es un estado transitorio: el cron reintentará en 1 minuto.
                if '[PICKING]' in msg or 'despacho' in msg.lower():
                    _logger.info(
                        'Picking %s: numero.despacho aún no disponible '
                        '(%s), se reintentará en el próximo ciclo.',
                        picking.name, msg.strip())
                else:
                    _logger.error(
                        'Picking %s: error de validación: %s',
                        picking.name, msg.strip())
            except Exception:
                _logger.exception('Error al validar picking %s.',
                                  picking.name)
        return validated
