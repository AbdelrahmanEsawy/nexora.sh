FROM odoo:19.0

# Replace core sources with your edited Odoo sources from odoo-core/src.
# Expected tree:
#   odoo-core/src/odoo
#   odoo-core/src/addons
COPY src/odoo /usr/lib/python3/dist-packages/odoo
COPY src/addons /usr/lib/python3/dist-packages/odoo/addons
