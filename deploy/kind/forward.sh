#!/usr/bin/env bash
# Port-forward the cluster's UIs to the Codespace (Codespaces then forwards them to a browser tab).
# Ctrl-C stops all of them.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
kubectl -n retail port-forward svc/chatbot 8000:8000 >/dev/null &
kubectl -n argocd port-forward svc/argocd-server 8080:80 >/dev/null &
kubectl -n retail port-forward svc/grafana 3000:3000 >/dev/null &
kubectl -n retail port-forward svc/prometheus 9090:9090 >/dev/null &
echo "chatbot :8000  argocd :8080  grafana :3000  prometheus :9090"
wait
