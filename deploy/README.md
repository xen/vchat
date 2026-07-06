# Production deployment

Runtime deployment files live under `deploy/` so the project root stays focused on
application code.

## Image

Build the runtime image from the repository root:

```bash
docker build -f deploy/Dockerfile -t vchat:local .
```

The image creates `/app/venv` and runs all Python commands from that virtualenv.

## Local compose smoke test

```bash
docker compose -f deploy/compose.yaml up --build
```

Compose uses ephemeral Postgres and Redis containers and writes `local.yaml` into
the app containers through a Compose config. The compose smoke test runs with
`mode: stage`; production deployments must set real security keys.

## Kubernetes settings

Kubernetes settings are passed as `/app/local.yaml`:

- non-secret values live in `deploy/k8s/base/configmap.yaml`;
- secret values live in a Kubernetes `Secret`;
- `local.yaml` uses the existing project `!env "$NAME"` syntax for sensitive
  values, so the same config file can reference `envFrom.secretRef`.
- environment variables are also applied as direct overrides after `local.yaml`
  is loaded; production mode fails fast if security keys are default,
  empty, or `change-me`.

Create a real secret from `secret.example.yaml` before applying to a cluster:

```bash
kubectl -n vchat create secret generic vchat-secret \
  --from-literal=DATABASE_URI='postgresql+asyncpg://...' \
  --from-literal=GIGACHAT_API_KEY='...' \
  --from-literal=SECRET_KEY='...' \
  --from-literal=COOKIE_KEY='...'
```

Then set the production image in `deploy/k8s/base/kustomization.yaml` or through
an overlay.

## Kubernetes apply

Base deployment:

```bash
kubectl apply -k deploy/k8s/base
```

If KEDA is installed, use the KEDA overlay to scale Celery workers by Redis queue
length:

```bash
kubectl apply -k deploy/k8s/keda
```

If Prometheus Operator is installed, apply the monitoring overlay:

```bash
kubectl apply -k deploy/k8s/monitoring
```

The monitoring overlay also starts a private Grafana instance with the VChat
dashboard provisioned from
`deploy/k8s/monitoring/grafana/dashboards/vchat-grafana.json`.
Before applying it in a shared cluster, create admin credentials:

```bash
kubectl -n vchat create secret generic vchat-grafana-secret \
  --from-literal=admin-user='admin' \
  --from-literal=admin-password='change-this-password'
```

The bundled datasource defaults to
`http://prometheus-operated.monitoring.svc:9090`. If the cluster uses another
Prometheus service name, patch `GF_DATASOURCE_PROMETHEUS_URL` in
`deploy/k8s/monitoring/grafana-deployment.yaml` or in an environment-specific
overlay.

Grafana is exposed as a `ClusterIP` service only:

```bash
kubectl -n vchat port-forward svc/vchat-grafana 3000:3000
```

## Metrics exposure

The app still exposes `/metrics` on the container port. In Kubernetes it is meant
to be scraped through the internal `vchat-metrics` ClusterIP service.

The public Ingress routes exact `/metrics` and prefix `/metrics/` to
`vchat-blackhole`, a service with no pods behind it, before routing `/` to the
web service. Keep that rule or an equivalent controller-level deny rule in any
environment-specific Ingress overlay.

## Container security controls

The runtime image and Kubernetes manifests assume these production controls:

- the image runs as UID/GID `10001`;
- pods set `runAsNonRoot`, fixed `runAsUser`/`runAsGroup`,
  `allowPrivilegeEscalation=false`, `readOnlyRootFilesystem=true`, dropped Linux
  capabilities, and RuntimeDefault seccomp;
- writable runtime state is limited to `emptyDir` mounted at `/tmp`;
- ingress to the web pods is restricted by `NetworkPolicy`;
- registry immutability and vulnerability scanning should be enforced by the
  CI/registry layer, for example Trivy scan before push and immutable image tags
  in Harbor or the target registry.
