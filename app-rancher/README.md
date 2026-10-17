# app-rancher

The Rancher 2.15.2 management server: the upstream chart from `releases.rancher.com/server-charts/stable`, installed by Zarf with Helm directly. No Flux, no reconciler - so nothing in this package waits on a GitOps engine, and `requires.fluxcd` is absent from its prerequisites document for that reason.

## TLS is `source=secret`, deliberately

The chart renders a cert-manager `Issuer` and `Certificate` only when `ingress.tls.source` is `rancher` or `letsEncrypt`. Setting it to `secret` removes cert-manager - its CRDs, its webhook, its four images - from the air gap completely. Create the secrets first:

```bash
kubectl -n cattle-system create secret tls tls-rancher-ingress \
  --cert=tls.crt --key=tls.key
# only if the cert is signed by a private CA, with privateCA: true in values:
kubectl -n cattle-system create secret generic tls-ca --from-file=cacerts.pem
```

If you switch to `source=rancher`, note that the `PackagePrerequisites` format **cannot express a cert-manager dependency**: `requires` permits only `kubernetes`, `fluxcd`, `storage` and `packages`, and CRDs can only be required under `fluxcd`. The nearest honest statement is a `requires.packages` entry naming whichever Zarf package installs cert-manager.

## The image list is partly hand-maintained

`zarf dev find-images . --kube-version 1.33.0` finds two images, `rancher` and `shell`. The bare command fails: Zarf templates at 1.37.0, and the chart declares `kubeVersion: < 1.37.0-0`.

The other four - `rancher-webhook`, `fleet`, `fleet-agent`, `remotedialer-proxy` - are created by Rancher *after* install, so no chart rendering reveals them. They still have to be in this package, because the Zarf agent rewrites every pod's image reference to the Zarf registry; an image Rancher pulls that was never pushed there becomes an `ImagePullBackOff` with no deployment error to explain it. Expect to add to this list the first time you deploy, from whatever you see pending.

## Versions here are script-synced

This package is hand-written for its chart, values and comments, but do not hand-edit its version strings or image tags: `../hack/split-images.py <version>` rewrites `metadata.version`, the chart version, every `docker.io/rancher/...` tag in `zarf.yaml`, and the two versions in `docs/prerequisites.yaml`, taking the tags from that release's own `rancher-images.txt`. It prints each tag it moved, and exits non-zero if an image listed here is absent from the new release or if a version line it expects has stopped matching.

What it does *not* touch is the Kubernetes ceiling in `docs/prerequisites.yaml`, which comes from the chart's `kubeVersion` and has to be re-read from the chart on a minor bump.

## Deploy

```bash
zarf package create . --confirm
zarf package deploy zarf-package-app-rancher-amd64-2.15.2.tar.zst
```

`RANCHER_HOSTNAME` and `RANCHER_INGRESS_CLASS` are prompted. Everything else is in [`values/rancher.yaml`](values/rancher.yaml).

`systemDefaultRegistry` is left empty there on purpose: Rancher hands that value to every downstream cluster it provisions, and Zarf's internal registry is a nodeport only the management cluster's own kubelets can reach. Set it to a routable registry - the one you mirrored the `mirror-rancher-*` packages into - or leave it empty if this Rancher manages only its local cluster.

## What it requires, and what it provides

[`docs/prerequisites.yaml`](docs/prerequisites.yaml) states a Kubernetes ceiling of `<1.37.0` and the `init` package, and nothing else. The supported *floor* lives in the SUSE support matrix and is deliberately not stated: the checker evaluates one comparator and one version, a two-ended range lands in `zarf_prerequisites_skipped`, and the ceiling is the only end the chart itself enforces.

It provides the `rancher-management-server` capability, the `management.cattle.io`, `provisioning.cattle.io` and `fleet.cattle.io` CRDs a downstream package would key off, and the `cattle-system` and `cattle-fleet-system` namespaces.
