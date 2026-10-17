# zarf-app-rancher

Rancher 2.15.2 as Zarf packages, for a cluster with no route to the internet.

Start with [`docs/using-this-repo.md`](docs/using-this-repo.md) for the end-to-end path; this page is the reference.

Two packages deploy something: Rancher itself, and a pull-only route to the registry `zarf init` leaves behind. Seven more carry the images Rancher pulls *later* - when it provisions a downstream cluster, or when somebody enables the monitoring chart - and deploy nothing at all. They are separate packages because their lifecycles are separate: the mirrored sets churn with the Kubernetes versions you provision, while `app-rancher` churns with the chart. Welding them together means re-shipping 12 GB to pick up a Rancher patch release.

One directory per package:

| Directory                       | Images | What it carries                                             |
| ------------------------------- | ------ | ----------------------------------------------------------- |
| `app-rancher/`                  | 6      | the chart, and the images Rancher itself runs               |
| `app-registry/`                 | 1      | a pull-only external route to the Zarf registry (3 flavors) |
| `mirror-rancher-provisioning/`  | 53     | node agents, RKE2/K3s runtime, upgrade controllers, CAPI    |
| `mirror-rancher-cni/`           | 34     | Calico, Cilium, multus, whereabouts, flannel                |
| `mirror-rancher-longhorn/`      | 26     | Longhorn and its CSI sidecars                               |
| `mirror-rancher-observability/` | 22     | the monitoring and logging charts                           |
| `mirror-rancher-cloudprovider/` | 13     | AKS/EKS/GKE operators, vSphere, Harvester, kube-vip         |
| `mirror-rancher-security/`      | 11     | NeuVector, CIS scans, compliance operator                   |
| `mirror-rancher-support/`       | 9      | backup-restore, SCC, supportability-review, Elemental       |
| `hack/split-images.py`          | -      | generates all seven `mirror-*` packages                     |
| `schema/`                       | -      | the `PackagePrerequisites` schema, one copy shared by all   |
| `docs/`                         | -      | the end-to-end guide                                        |

Every package's `documentation.schema` points at `../schema/`, so the schema lives once and is still packaged into each tarball: Zarf resolves a documentation path that climbs out of the package directory, and `zarf package inspect documentation` extracts the document and its schema side by side. The `$schema` line inside each `docs/prerequisites.yaml` is `../../schema/...` - that one is for the editor you write the file in, and does not resolve against the flat extracted copy.

## Build

```bash
zarf package create app-rancher --confirm
zarf package create app-registry -f nginx --confirm
zarf package create mirror-rancher-provisioning --confirm
```

These are not small. `mirror-rancher-support` is the lightest at 9 images and builds to **811 MB**; budget accordingly before pulling all 178.

Only `app-rancher` and `app-registry` are hand-written. The seven `mirror-*` packages are generated whole. Either way a new Rancher release is one command:

```bash
./hack/split-images.py 2.15.3
```

It fetches that release's `rancher-images.txt`, partitions it across the eight groups, rewrites each generated `zarf.yaml`, `docs/prerequisites.yaml` and `README.md`, and syncs `app-rancher`'s version strings and image tags in place rather than leaving the hand-written package behind at the old release. It prints every tag it moved. A version string whose line stopped matching is a hard failure too, not a silent no-op. **An image matching no group makes it exit non-zero** rather than fall into a catch-all - that is how a chart added in the next release announces itself, instead of silently landing in whichever package sorted first. The group table at the top of the script is the only part a human maintains.

## Deploy

```bash
zarf package deploy zarf-package-app-rancher-amd64-2.15.2.tar.zst
```

Before that, the cluster needs a `tls-rancher-ingress` secret in `cattle-system` (and `tls-ca` if you set `privateCA`), because this package uses `ingress.tls.source=secret` - which is what keeps cert-manager out of the air gap entirely. It also needs an ingress controller, and a Kubernetes version **below 1.37.0**, which is what the chart's own `kubeVersion: < 1.37.0-0` enforces.

`RANCHER_HOSTNAME` and `RANCHER_INGRESS_CLASS` are prompted at deploy time.

## Mirror the rest

The `mirror-*` packages belong in the registry your *downstream* clusters can reach - not Zarf's internal nodeport registry, which only the management cluster's kubelets can see:

```bash
zarf package mirror-resources zarf-package-mirror-rancher-provisioning-amd64-2.15.2.tar.zst \
  --components common,k8s-1-35 \
  --registry-url registry.example.com --registry-push-username <user>
```

Then point Rancher's `systemDefaultRegistry` at that same address. Inside `mirror-rancher-provisioning`, the per-minor components (`k8s-1-34`, `k8s-1-35`, `k8s-1-36`) hold the images tagged per Kubernetes version - `rke2-runtime` and `hardened-kubernetes` among them, the heavy ones. Selecting only the minors you actually provision is the largest single saving in the whole set.

## Check a cluster before deploying

Every package ships a `PackagePrerequisites` document under its `prerequisites` documentation key, extractable without deploying anything:

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

Print `zarf_prerequisites_skipped` on a successful run too - a run that showed only its passes reads as though the whole document had been verified. Deploying several packages in order means checking *inside* the loop, since an earlier package is what satisfies a later one's requirements.

Nothing here `requires` a `mirror-*` package. If you mirror one into an external registry instead of deploying it, `zarf package list` never reports it, so such a requirement could never be satisfied - and Rancher installs and runs perfectly well without a single downstream image.
