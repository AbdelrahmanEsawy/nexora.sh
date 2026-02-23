#!/usr/bin/env python3
import argparse
import base64
import json
import os
import subprocess
import sys
from textwrap import dedent


def run(cmd, input_text=None):
    res = subprocess.run(
        cmd,
        input=input_text.encode("utf-8") if input_text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr.decode("utf-8"))
        raise SystemExit(res.returncode)
    return res.stdout.decode("utf-8")


def kubectl_base_args(kubeconfig):
    return ["kubectl", f"--kubeconfig={kubeconfig}"]


def get_secret(namespace, name, kubeconfig):
    out = run(kubectl_base_args(kubeconfig) + ["-n", namespace, "get", "secret", name, "-o", "json"])
    data = json.loads(out)
    def get(key, default=None):
        if key not in data.get("data", {}):
            if default is not None:
                return default
            raise SystemExit(f"Missing key {key} in secret/{name}")
        return base64.b64decode(data["data"][key]).decode("utf-8")
    return data, get


def main():
    ap = argparse.ArgumentParser(description="Provision a per-env Odoo deployment with its own subdomain.")
    ap.add_argument("--project", required=True, help="Project slug, e.g. new or acme")
    ap.add_argument("--env", default="prod", choices=["dev", "staging", "prod"], help="Environment name")
    ap.add_argument("--base-domain", default="nexora.red", help="Base domain")
    ap.add_argument(
        "--apex-env",
        default=os.environ.get("APEX_ENV", "dev"),
        choices=["dev", "staging", "prod"],
        help="Environment that should use apex host <slug>.<base-domain>",
    )
    ap.add_argument("--namespace", default="odoo-system", help="Kubernetes namespace")
    ap.add_argument("--db-name", default=None, help="Database name override")
    ap.add_argument("--admin-passwd", default=None, help="Odoo master password (admin_passwd)")
    ap.add_argument(
        "--storage-class",
        default=os.environ.get("STORAGE_CLASS_NAME", "csi-cinder-high-speed"),
        help="StorageClassName for PVCs (empty to use cluster default)",
    )
    ap.add_argument(
        "--tls-issuer",
        default=os.environ.get("TLS_CLUSTER_ISSUER", "letsencrypt-prod"),
        help="cert-manager ClusterIssuer name (empty to skip issuer annotation)",
    )
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", "/tmp/nexora-kubeconfig"))
    ap.add_argument("--dry-run", action="store_true", help="Print manifest only")
    args = ap.parse_args()

    if not args.admin_passwd:
        args.admin_passwd = os.environ.get("ODOO_ADMIN_PASSWD")
    if not args.admin_passwd:
        raise SystemExit("Missing --admin-passwd or ODOO_ADMIN_PASSWD env var")

    project = args.project.strip().lower()
    env = args.env
    if env == args.apex_env:
        host = f"{project}.{args.base_domain}"
    else:
        host = f"{project}--{env}.{args.base_domain}"

    db_name = args.db_name or f"{project}_{env}"

    data, get = get_secret(args.namespace, "nexora-db", args.kubeconfig)
    db_host = get("host")
    db_port = get("port")
    db_user = get("user")
    db_password = get("password")
    db_sslmode = get("sslmode", default="require")

    name = f"odoo-{project}-{env}"
    pvc_name = f"odoo-filestore-{project}-{env}"
    cfg_secret = f"odoo-config-{project}-{env}"
    storage_class_line = f"  storageClassName: {args.storage_class}\n" if args.storage_class else ""
    issuer = (args.tls_issuer or "").strip()
    issuer_annotation = f"\n        cert-manager.io/cluster-issuer: {issuer}" if issuer else ""

    manifest = dedent(f"""
    apiVersion: v1
    kind: Secret
    metadata:
      name: {cfg_secret}
      namespace: {args.namespace}
    type: Opaque
    stringData:
      odoo.conf: |
        [options]
        admin_passwd = {args.admin_passwd}
        db_host = {db_host}
        db_port = {db_port}
        db_user = {db_user}
        db_password = {db_password}
        db_name = {db_name}
        list_db = False
        proxy_mode = True
    ---
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: {pvc_name}
      namespace: {args.namespace}
    spec:
      accessModes:
        - ReadWriteOnce
    {storage_class_line}  resources:
        requests:
          storage: 10Gi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: {name}
      namespace: {args.namespace}
      labels:
        app: odoo
        project: {project}
        env: {env}
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: odoo
          project: {project}
          env: {env}
      template:
        metadata:
          labels:
            app: odoo
            project: {project}
            env: {env}
        spec:
          securityContext:
            fsGroup: 101
          initContainers:
            - name: init-permissions
              image: busybox:1.36
              command: ["sh", "-c", "chown -R 101:101 /var/lib/odoo"]
              volumeMounts:
                - name: filestore
                  mountPath: /var/lib/odoo
          containers:
            - name: odoo
              image: odoo:19
              imagePullPolicy: IfNotPresent
              ports:
                - containerPort: 8069
                  name: http
              env:
                - name: HOST
                  value: {db_host}
                - name: PORT
                  value: "{db_port}"
                - name: USER
                  value: {db_user}
                - name: PASSWORD
                  value: {db_password}
                - name: PGSSLMODE
                  value: {db_sslmode}
              volumeMounts:
                - name: filestore
                  mountPath: /var/lib/odoo
                - name: odoo-config
                  mountPath: /etc/odoo/odoo.conf
                  subPath: odoo.conf
          volumes:
            - name: filestore
              persistentVolumeClaim:
                claimName: {pvc_name}
            - name: odoo-config
              secret:
                secretName: {cfg_secret}
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: {name}
      namespace: {args.namespace}
      labels:
        app: odoo
        project: {project}
        env: {env}
    spec:
      type: ClusterIP
      selector:
        app: odoo
        project: {project}
        env: {env}
      ports:
        - name: http
          port: 80
          targetPort: 8069
    ---
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: {name}
      namespace: {args.namespace}
      labels:
        app: odoo
        project: {project}
        env: {env}
      annotations:
        nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
        nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
        nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"{issuer_annotation}
    spec:
      ingressClassName: nginx
      tls:
        - hosts:
            - {host}
          secretName: {name}-tls
      rules:
        - host: {host}
          http:
            paths:
              - path: /
                pathType: Prefix
                backend:
                  service:
                    name: {name}
                    port:
                      number: 80
    """).strip() + "\n"

    if args.dry_run:
        print(manifest)
        return

    run(kubectl_base_args(args.kubeconfig) + ["apply", "-f", "-"], input_text=manifest)
    print(f"Provisioned {project}/{env} at https://{host}")


if __name__ == "__main__":
    main()
