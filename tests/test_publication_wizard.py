from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestMeliPublicationWizardPricing(TransactionCase):
    """Verifica fórmulas de precio del wizard de publicación MELI."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product'].create({
            'name': 'Producto Test MELI',
            'type': 'product',
            'list_price': 0.0,
        })

    def _make_wizard(self, price_usd, dolar_rogrim):
        return self.env['meli.publication.wizard'].create({
            'product_id': self.product.id,
            'qty_to_transfer': 1.0,
            'price_usd': price_usd,
            'dolar_rogrim': dolar_rogrim,
            'precio_final': 0.0,
        })

    def test_precio_neto_formula(self):
        """USD=10, dolar=1000 → 10 * 1.21 * 1.14 * 1000 = 13794"""
        wizard = self._make_wizard(10, 1000)
        self.assertAlmostEqual(wizard.precio_neto, 13794.0, places=2)

    def test_precio_neto_cero_si_usd_cero(self):
        wizard = self._make_wizard(0, 1200)
        self.assertEqual(wizard.precio_neto, 0)
        self.assertEqual(wizard.gasto_fijo, 0)
        self.assertEqual(wizard.precio_base, 0)

    def test_gasto_fijo_tramo_1(self):
        wizard = self._make_wizard(10, 1000)  # neto = 13794
        self.assertEqual(wizard.gasto_fijo, 1000)

    def test_gasto_fijo_tramo_2(self):
        wizard = self._make_wizard(15, 1000)  # neto = 20691
        self.assertEqual(wizard.gasto_fijo, 2000)

    def test_gasto_fijo_tramo_3(self):
        wizard = self._make_wizard(30, 1000)  # neto = 41382
        self.assertEqual(wizard.gasto_fijo, 7500)

    def test_borde_inferior_tramo_2(self):
        """precio_neto exactamente 15000 → gasto 2000"""
        wizard = self._make_wizard(1, 15000 / 1.21 / 1.14)
        self.assertAlmostEqual(wizard.precio_neto, 15000, places=2)
        self.assertEqual(wizard.gasto_fijo, 2000)

    def test_borde_superior_tramo_2(self):
        """precio_neto exactamente 33000 → gasto 2000"""
        wizard = self._make_wizard(1, 33000 / 1.21 / 1.14)
        self.assertAlmostEqual(wizard.precio_neto, 33000, places=2)
        self.assertEqual(wizard.gasto_fijo, 2000)

    def test_precio_base(self):
        """precio_base = precio_neto + gasto_fijo"""
        wizard = self._make_wizard(10, 1000)  # neto=13794, gasto=1000
        self.assertAlmostEqual(wizard.precio_base, 14794.0, places=2)

    def test_precio_base_tramo_3(self):
        wizard = self._make_wizard(30, 1000)  # neto=41382, gasto=7500
        self.assertAlmostEqual(wizard.precio_base, 48882.0, places=2)


@tagged('post_install', '-at_install')
class TestMeliPublicationWizardValidations(TransactionCase):
    """Verifica las validaciones de action_confirm del wizard."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product_publicado = cls.env['product.product'].create({
            'name': 'Producto Ya Publicado',
            'type': 'product',
            'list_price': 5000.0,
        })

    def test_no_permitir_producto_ya_publicado(self):
        """Si list_price > 1, action_confirm debe levantar UserError."""
        wizard = self.env['meli.publication.wizard'].create({
            'product_id': self.product_publicado.id,
            'qty_to_transfer': 1.0,
            'price_usd': 10.0,
            'dolar_rogrim': 1000.0,
            'precio_final': 14794.0,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_precio_final_cero_no_permitido(self):
        """precio_final <= 0 debe levantar UserError."""
        product_nuevo = self.env['product.product'].create({
            'name': 'Producto Nuevo',
            'type': 'product',
            'list_price': 0.0,
        })
        wizard = self.env['meli.publication.wizard'].create({
            'product_id': product_nuevo.id,
            'qty_to_transfer': 1.0,
            'price_usd': 10.0,
            'dolar_rogrim': 1000.0,
            'precio_final': 0.0,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()
