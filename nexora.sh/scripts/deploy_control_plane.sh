#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG=${KUBECONFIG:-/tmp/nexora-kubeconfig}
APP_SECRET=${APP_SECRET:-}
ODOO_ADMIN_PASSWD=${ODOO_ADMIN_PASSWD:-}

if [[ -z "$APP_SECRET" || -z "$ODOO_ADMIN_PASSWD" ]]; then
  echo "APP_SECRET and ODOO_ADMIN_PASSWD are required" >&2
  exit 1
fi

kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/namespace.yaml
kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/rbac.yaml
echo "Skipping PVC (using emptyDir for control-plane data)"

kubectl --kubeconfig "$KUBECONFIG" -n nexora-system create secret generic nexora-control-plane \
  --from-literal=APP_SECRET="$APP_SECRET" \
  --from-literal=ODOO_ADMIN_PASSWD="$ODOO_ADMIN_PASSWD" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG" apply -f -

kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/deployment.yaml
kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/service.yaml
kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/ingress.yaml

if kubectl --kubeconfig "$KUBECONFIG" get crd clusterissuers.cert-manager.io >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/cluster-issuer-letsencrypt.yaml
  kubectl --kubeconfig "$KUBECONFIG" apply -f k8s/control-plane/certificate-app-nexora-red.yaml
else
  echo "cert-manager CRDs not found; skipping ClusterIssuer/Certificate apply"
fi

kubectl --kubeconfig "$KUBECONFIG" -n nexora-system rollout restart deployment/nexora-control-plane
kubectl --kubeconfig "$KUBECONFIG" -n nexora-system rollout status deployment/nexora-control-plane --timeout=300s
