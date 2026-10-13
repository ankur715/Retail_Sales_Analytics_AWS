#!/usr/bin/env bash
# Bring up the milestone 3 stack in the Codespace (never on the laptop):
#   kind cluster -> chatbot image loaded into it -> Argo CD -> the retail-chatbot Application,
#   which syncs deploy/k8s from GitHub at REVISION (default: the current branch, so it must be pushed).
#
#   bash deploy/kind/up.sh            # first time ~5 min; safe to re-run
#   bash deploy/kind/forward.sh       # port-forward chatbot :8000, Argo CD :8080, Grafana :3000, Prometheus :9090
#   kind delete cluster --name retail # when done (frees ~3 GB)
set -euo pipefail
cd "$(dirname "$0")/../.."

KIND_VERSION=v0.25.0
KUBECTL_VERSION=v1.31.2
ARGOCD_VERSION=v2.13.1
REVISION=${REVISION:-$(git branch --show-current)}
BIN="$HOME/.local/bin"
mkdir -p "$BIN"; export PATH="$BIN:$PATH"

if ! command -v kind >/dev/null; then
  curl -fsSLo "$BIN/kind" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64" && chmod +x "$BIN/kind"
fi
if ! command -v kubectl >/dev/null; then
  curl -fsSLo "$BIN/kubectl" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && chmod +x "$BIN/kubectl"
fi

kind get clusters | grep -qx retail || kind create cluster --config deploy/kind/cluster.yaml --wait 120s

# Argo CD syncs manifests, not code: the image is built here and loaded into the node.
docker build -q -t retail-chatbot:local -f docker/chatbot.Dockerfile .
kind load docker-image retail-chatbot:local --name retail

kubectl get ns argocd >/dev/null 2>&1 || kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml" >/dev/null
kubectl apply --server-side --force-conflicts -f deploy/argocd/argocd-cm.yaml >/dev/null
kubectl -n argocd rollout restart deploy/argocd-server deploy/argocd-repo-server >/dev/null
kubectl -n argocd rollout status deploy/argocd-server deploy/argocd-repo-server --timeout=300s
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=300s

sed "s|targetRevision: dev|targetRevision: ${REVISION}|" deploy/argocd/application.yaml | kubectl apply -f - >/dev/null
echo "Argo CD is syncing deploy/k8s@${REVISION} ..."
for _ in $(seq 1 60); do
  status=$(kubectl -n argocd get application retail-chatbot -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null || true)
  [ "$status" = "Synced/Healthy" ] && break
  sleep 5
done
echo "retail-chatbot: ${status:-unknown}"
# New pods pick up a freshly loaded image (same tag). Deleting pods, rather than `rollout restart`,
# leaves the Deployment spec untouched, so Argo CD stays Synced.
kubectl -n retail delete pod -l 'app in (chatbot,sqlgen)' --wait=false >/dev/null
kubectl -n retail rollout status deploy/chatbot deploy/sqlgen --timeout=180s
kubectl -n retail get pods
