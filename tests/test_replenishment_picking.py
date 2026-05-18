from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestReplenishmentPickingReservation(TransactionCase):
    """Verifica que el picking R/S → MELI reserva el stock correctamente.

    Cubre el bug por el que se desincronizaban los despachos: el helper
    _create_replenishment_picking borraba las move_lines reservadas por
    action_assign() y las recreaba a mano, dejando el stock de R/S sin
    reservar (reserved_quantity = 0). Otro despacho podía entonces tomar
    las mismas unidades.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product'].create({
            'name': 'Producto Reserva Test',
            'type': 'product',
        })

        warehouse = cls.env['stock.warehouse'].search([], limit=1)
        cls.rs_loc = cls.env['stock.location'].create({
            'name': 'R/S Test',
            'usage': 'internal',
            'location_id': warehouse.view_location_id.id,
        })
        cls.rs_subloc = cls.env['stock.location'].create({
            'name': 'Sub R/S Test',
            'usage': 'internal',
            'location_id': cls.rs_loc.id,
        })
        cls.meli_loc = cls.env['stock.location'].create({
            'name': 'MELI Stock Test',
            'usage': 'internal',
            'location_id': warehouse.view_location_id.id,
        })

        # Garantizar un tipo de operación interno para el helper.
        picking_type = cls.env['stock.picking.type'].search(
            [('code', '=', 'internal')], limit=1)
        if not picking_type:
            cls.env['stock.picking.type'].create({
                'name': 'Internal Test',
                'code': 'internal',
                'sequence_code': 'INT-T',
                'warehouse_id': warehouse.id,
                'default_location_src_id': cls.rs_loc.id,
                'default_location_dest_id': cls.meli_loc.id,
            })

        # 20 unidades disponibles en la sub-ubicación de R/S.
        cls.env['stock.quant']._update_available_quantity(
            cls.product, cls.rs_subloc, 20.0)

    def _quant_rs(self):
        return self.env['stock.quant'].search([
            ('product_id', '=', self.product.id),
            ('location_id', '=', self.rs_subloc.id),
        ])

    def test_picking_reserva_stock_en_rs(self):
        """El picking debe RESERVAR el stock en R/S, no dejarlo libre."""
        Rule = self.env['meli.replenishment.rule']
        picking = Rule._create_replenishment_picking(
            self.product, 5.0, meli_loc=self.meli_loc, rs_loc=self.rs_loc)

        self.assertTrue(picking, 'Debe crearse el picking.')
        self.assertEqual(picking.state, 'assigned',
                         'El picking debe quedar reservado (assigned).')

        # Verificación central del fix: el quant de R/S quedó reservado.
        self.assertEqual(
            self._quant_rs().reserved_quantity, 5.0,
            'El stock a transferir debe quedar RESERVADO en R/S. Si es 0, '
            'un despacho a cliente podría tomar las mismas unidades.')

        # El disponible bajó de 20 a 15.
        self.assertEqual(
            self.env['stock.quant']._get_available_quantity(
                self.product, self.rs_subloc),
            15.0)

    def test_segundo_picking_no_toma_stock_reservado(self):
        """Un segundo picking sólo puede tomar el stock NO reservado."""
        Rule = self.env['meli.replenishment.rule']
        Rule._create_replenishment_picking(
            self.product, 5.0, meli_loc=self.meli_loc, rs_loc=self.rs_loc)

        # Quedan 15 disponibles; pedir 20 sólo debe tomar 15.
        picking2 = Rule._create_replenishment_picking(
            self.product, 20.0, meli_loc=self.meli_loc, rs_loc=self.rs_loc)

        total = sum(picking2.move_ids.mapped('product_uom_qty'))
        self.assertEqual(
            total, 15.0,
            'El segundo picking sólo debe tomar las 15 u. libres, no las 5 '
            'ya reservadas por el primero.')

    def test_move_line_destino_correcto(self):
        """Las move_lines deben apuntar a la ubicación MELI de destino."""
        Rule = self.env['meli.replenishment.rule']
        picking = Rule._create_replenishment_picking(
            self.product, 5.0, meli_loc=self.meli_loc, rs_loc=self.rs_loc)

        move_lines = picking.move_ids.move_line_ids
        self.assertTrue(move_lines, 'El picking debe tener move_lines.')
        for line in move_lines:
            self.assertEqual(line.location_id, self.rs_subloc)
            self.assertTrue(
                line.location_dest_id == self.meli_loc
                or line.location_dest_id.id in self.meli_loc.child_ids.ids,
                'El destino de la move_line debe ser MELI/Stock o una '
                'sub-ubicación suya.')
