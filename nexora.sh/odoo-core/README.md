# Odoo Core Source

Use this folder when you want to modify Odoo core itself.

## Layout

- `src/odoo`: Odoo python package source
- `src/addons`: Odoo community addons source
- `Dockerfile`: builds a custom Odoo image from the sources above

## First setup

Run:

```bash
./scripts/bootstrap_odoo_core.sh
```

This clones Odoo 19.0 into `odoo-core/src`.

## Build and deploy flow

1. Edit core files under `odoo-core/src/...`
2. Commit and push to `main`
3. GitHub Actions workflow `Build and Push Custom Odoo Core` publishes:
   `ghcr.io/<your-org-or-user>/nexora-odoo-core:19.0-custom`
4. In Nexora Project Settings set **Odoo version or image** to that full image tag
5. Click **Redeploy** for the environment
