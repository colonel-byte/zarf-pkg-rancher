# Using this repo

Rancher 2.15.2 for an air-gapped cluster, as ten Zarf packages. This is the end-to-end path; each package's own `README.md` has the detail.

## What is here, and why it is split

Rancher pulls images at three different times, and they belong in different packages because they have different lifecycles.

| When                              | What pulls                               | Package                                             |
| --------------------------------- | ---------------------------------------- | --------------------------------------------------- |
| At install                        | the Rancher server, webhook, Fleet       | `app-rancher`                                       |
| When somebody enables a chart     | monitoring, logging, Longhorn, NeuVector | the matching `mirror-*`                             |
| When Rancher provisions a cluster | node agents, the RKE2/K3s runtime, CNI   | `mirror-rancher-provisioning`, `mirror-rancher-cni` |

Welding them into one package means re-shipping 12 GB to pick up a Rancher patch release. Keeping them apart means the mirrored sets move on their own schedule, which they measurably do: between 2.15.0 and 2.15.1, `mirror-rancher-support` went from 5 images to 9 and `mirror-rancher-longhorn` from 31 to 25, with no change to the Rancher chart at all.

| Package                        | Images | Built size | Deploys a workload? |
| ------------------------------ | ------ | ---------- | ------------------- |
| `app-rancher`                  | 6      | 1.2G       | yes                 |
| `app-registry`                 | 1      | 25M        | yes                 |
| `mirror-rancher-provisioning`  | 53     | 3.2G       | no                  |
| `mirror-rancher-observability` | 22     | 1.9G       | no                  |
| `mirror-rancher-longhorn`      | 26     | 1.8G       | no                  |
| `mirror-rancher-cni`           | 34     | 1.6G       | no                  |
| `mirror-rancher-security`      | 11     | 1.3G       | no                  |
| `mirror-rancher-support`       | 9      | 811M       | no                  |
| `mirror-rancher-cloudprovider` | 13     | 437M       | no                  |

Those sizes are measured, not estimated. The whole set is 12 GB.

## The decision you make first

Whether this Rancher provisions **other** clusters. It changes what you carry and what you deploy.

**Rancher managing only its own cluster.** Build `app-rancher`, deploy it, stop. Leave `systemDefaultRegistry` empty. None of the `mirror-*` packages are needed, and neither is `app-registry`. This is about 1.2 GB.

**Rancher provisioning a fleet.** Downstream kubelets pull from an address they can reach, which Zarf's internal registry is not: it is a NodePort serving plain HTTP that only the management cluster's own kubelets can see. So you also need a way to expose it (`app-registry`) and the mirrored images in it, and `systemDefaultRegistry` set to that address.

## Build

```bash
zarf package create app-rancher --confirm
zarf package create app-registry -f nginx --confirm
zarf package create mirror-rancher-provisioning --confirm
```

Only `app-rancher` and `app-registry` are hand-written. The seven `mirror-*` packages are generated:

```bash
./hack/split-images.py 2.15.3
```

That fetches the release's `rancher-images.txt`, partitions all 178 images across eight groups, rewrites every generated `zarf.yaml`, `docs/prerequisites.yaml` and `README.md`, and syncs `app-rancher`'s version strings and image tags in place. **An image matching no group exits non-zero** rather than landing in a catch-all, which is how a chart added upstream announces itself. The group table at the top of the script is the only part a human maintains.

Tested across the three real 2.15.x releases: no unmatched images on any of them, and re-running at the starting version returns the tree byte-identical.

## Check before you deploy

Every package carries a `PackagePrerequisites` document, readable without deploying anything:

```bash
zarf package inspect documentation zarf-package-app-rancher-amd64-2.15.2.tar.zst
```

```yaml
- name: Check the cluster against what this package requires
  ansible.builtin.include_role:
    name: colonel_byte.zarf.prerequisites
  vars:
    zarf_package: /srv/staging/zarf-package-app-rancher-amd64-2.15.2.tar.zst
    zarf_kubeconfig: /etc/rancher/rke2/rke2.yaml
```

Print `zarf_prerequisites_skipped` on a successful run too: a run that showed only its passes reads as though the whole document had been verified. Deploying several packages in order means checking *inside* the loop, since an earlier package is what satisfies a later one's requirements.

**What these documents cannot say.** `requires` permits only `kubernetes`, `fluxcd`, `storage` and `packages`, and CRDs are requirable only under `fluxcd`. So "needs cert-manager", "needs an ingress controller", "needs Gateway API" are all unstatable, and live in prose in the package READMEs instead. `app-registry` is the sharpest case: its three flavors need three different routers, and because `documentation:` is package-level, all three flavors ship the same document.

## Deploy

```bash
zarf init --registry-pvc-size 40Gi          # 20Gi default is smaller than this image set
zarf package deploy zarf-package-app-rancher-amd64-2.15.2.tar.zst
```

Rancher needs a `tls-rancher-ingress` secret in `cattle-system` first. This repo sets `ingress.tls.source=secret`, which keeps cert-manager out of the air gap entirely - the chart only renders a cert-manager `Issuer` when the source is `rancher` or `letsEncrypt`. It also needs a Kubernetes version **below 1.37.0**, which the chart's own `kubeVersion: < 1.37.0-0` enforces.

## Mirror the rest, and expose it

```bash
zarf package deploy zarf-package-app-registry-amd64-1.0.0-nginx.tar.zst

zarf package mirror-resources zarf-package-mirror-rancher-provisioning-amd64-2.15.2.tar.zst \
  --components common,k8s-1-35 \
  --registry-url registry.example.com --registry-push-username <user>
```

`--components` is worth using: inside `mirror-rancher-provisioning`, the `k8s-1-34`, `k8s-1-35` and `k8s-1-36` components each hold the seven images tagged per Kubernetes version, including the heavy `rke2-runtime` and `hardened-kubernetes`. Two of the three are dead weight for any given deployment.

Then set Rancher's `systemDefaultRegistry` to the `app-registry` hostname, and distribute the Zarf pull credentials (`zarf tools get-creds`) and your CA to provisioned clusters through Rancher's `registries.yaml` support in the RKE2/K3s cluster spec.

## Gotchas that cost real time

**The Zarf agent rewrites image references cluster-wide.** Anything Rancher creates *after* install - `rancher-webhook`, `fleet`, `fleet-agent`, `remotedialer-proxy` - gets rewritten to the Zarf registry. If those images were never pushed there, you get `ImagePullBackOff` with no deployment error to explain it. They are listed by hand in `app-rancher/zarf.yaml` for exactly this reason, and `zarf dev find-images` cannot find them because no chart rendering reveals them.

**`zarf dev find-images` needs `--kube-version`.** Zarf templates at 1.37.0, which the Rancher chart refuses. Use `--kube-version 1.33.0`.

**`systemDefaultRegistry` is inherited by downstream clusters.** Never point it at Zarf's internal nodeport registry; downstream nodes cannot reach it.

**Nothing here `requires` a `mirror-*` package.** If you mirror one into an external registry rather than deploying it, `zarf package list` never reports it, so such a requirement could never be satisfied - and Rancher runs perfectly well without a single downstream image.

## Layout

```
app-rancher/            the chart, hand-written
app-registry/           the pull-only registry route, hand-written, three flavors
mirror-rancher-*/       seven generated image-only packages
hack/split-images.py    generates them
schema/                 the PackagePrerequisites schema, one copy shared by all
docs/                   this guide
```

Each package's `documentation.schema` points at `../schema/`, so the schema lives once and still ships inside every tarball: Zarf resolves a documentation path that climbs out of the package directory, and `zarf package inspect documentation` extracts the document and its schema side by side.
