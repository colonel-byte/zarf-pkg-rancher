# app-rancher-spoke

Joins an existing air-gapped cluster to a hub Rancher, and carries the images Rancher creates pods for *after* the join. Deployed on the **spoke**, not on the hub.

The join itself is one `kubectl apply` of a manifest the hub generates. What fails in an air gap is everything that happens next: the cluster agent, the `rancher-webhook` chart the agent installs here, the helm-operation pod that performs that install, and the Fleet agent are all pulled by this cluster, from Docker Hub, unless the images are already local.

This package is for a cluster that **already exists** - any distro, `zarf init` already run. A cluster Rancher provisions from scratch is a different problem: its nodes pull `rke2-runtime`, the hardened images and the CNI before there is a cluster to deploy a Zarf package into. Those live in `mirror-rancher-provisioning` and `mirror-rancher-cni`.

## Images, and what pulls them

| Image                               | Runs as                                                  | Component            |
| ----------------------------------- | -------------------------------------------------------- | -------------------- |
| `rancher-agent:v2.15.2`             | `cattle-cluster-agent` in `cattle-system`                | `agent`              |
| `rancher-webhook:v0.11.3`           | the `rancher-webhook` chart, installed here by the agent | `agent`              |
| `shell:v0.8.2`                      | the helm-operation pod that installs that chart          | `agent`              |
| `fleet-agent:v0.16.2`               | `fleet-agent` in `cattle-fleet-system`                   | `agent`              |
| `system-upgrade-controller:v0.20.1` | the SUC chart, if the hub manages this cluster's version | `version-management` |
| `kuberlr-kubectl:v8.1.1`            | that chart's kubectl container                           | `version-management` |
| `k3s-upgrade` / `rke2-upgrade`      | the upgrade jobs the SUC plans run, per Kubernetes minor | `k8s-1-34/35/36`     |

The four in `agent` are not optional and are not conditional on anything you configure - every imported cluster pulls all four. The rest are.

`kube-api-auth` is deliberately absent: the import manifest renders it only for the RKE1-era authorized cluster endpoint. So are the monitoring, logging, Longhorn and NeuVector images - if you enable those charts on this cluster, mirror the matching `mirror-*` package into it as well.

## The part that decides whether this works

Rancher, on the hub, decides which registry path the spoke's pods ask for. There are two ways to make that path one this cluster can serve, and which one you need depends on the hub.

**The hub's `systemDefaultRegistry` is empty.** Rancher hands out plain `rancher/...` references, Zarf's mutating agent rewrites them to this cluster's own registry, and a plain deploy is all it takes. This is the same mechanism `app-rancher` relies on for the images Rancher pulls after install.

```bash
zarf package deploy zarf-package-app-rancher-spoke-amd64-2.15.2.tar.zst \
  --components agent,register \
  --set RANCHER_IMPORT_URL=https://rancher.example.com/v3/import/abc123_c-m-xxxxx.yaml
```

**The hub's `systemDefaultRegistry` is set** (which it is, if that hub provisions a fleet). Then Rancher hands out `registry.example.com/rancher/...`, the Zarf agent's rewrite of that reference does not match what a plain deploy pushed, and the pods fail. Mirror to the registry this cluster actually reaches instead, with the tags unaltered:

```bash
zarf package mirror-resources zarf-package-app-rancher-spoke-amd64-2.15.2.tar.zst \
  --images --no-img-checksum \
  --registry-url registry.example.com --registry-push-username <user>

zarf package deploy zarf-package-app-rancher-spoke-amd64-2.15.2.tar.zst \
  --components register --set RANCHER_IMPORT_URL=...
```

`--no-img-checksum` is the whole point of that command: without it Zarf appends a checksum to every tag, and Rancher asks for the tag without one.

Per spoke, you can also override the hub's global registry: set `spec.importedConfig.privateRegistryURL` (and `privateRegistryPullSecrets`) on the cluster. A cluster-level registry wins over `systemDefaultRegistry`, and it reaches the agent, the downstream charts and Fleet alike - so one spoke can pull from a registry nothing else in the fleet can see.

## Variables

| Variable             | Component  | Meaning                                                                            |
| -------------------- | ---------- | ---------------------------------------------------------------------------------- |
| `RANCHER_IMPORT_URL` | `register` | the hub's import URL, from **Import Existing** in the UI. Carries a cluster token  |
| `IMPORT_CURL_OPTS`   | `register` | extra curl options, e.g. `--cacert /etc/ssl/hub-ca.pem`, or `-k` to skip the check |

Neither is prompted: Zarf prompts per package rather than per selected component, and a mirror-only run must not have to answer for a join it is not doing. Pass them with `--set`. Deploying `register` without a URL fails with a message rather than applying nothing.

The host running `zarf package deploy` needs `curl` and a route to the hub's ingress. `kubectl` it does not need - the action uses the one inside the Zarf binary.

## Verify the join

```bash
zarf tools kubectl -n cattle-system get deploy cattle-cluster-agent -o jsonpath='{..image}'
zarf tools kubectl -n cattle-fleet-system get pods
```

The agent's image should read as this cluster's registry, not `docker.io`. If a pod is in `ImagePullBackOff`, the reference it is asking for is the answer to which of the two modes above you are actually in. The helm-operation pod in `cattle-system` is where a missing `rancher/shell` shows up, and it is also where the webhook install's log lives.
