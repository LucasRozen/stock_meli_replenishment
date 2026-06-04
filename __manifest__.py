{
    'name': 'Reabastecimiento MELI desde R/S',
    'version': '17.0.3.0.0',
    'category': 'Inventory',
    'summary': 'Repone N unidades en MELI/Stock desde R/S cuando el stock llega a 0',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/meli_replenishment_views.xml',
        'views/meli_publication_wizard_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
