# app-registry

A pull-only external route to the registry `zarf init` already deployed, so downstream clusters can pull from it without a second registry in the cluster.

Three flavors, differing only in how traffic reaches the cluster:

| Flavor        | Routing object                               | Attaches to                              | Build                                  |
| ------------- | -------------------------------------------- | ---------------------------------------- | -------------------------------------- |
| `nginx`       | `Ingress`                                    | an ingress controller, by class          | `zarf package create . -f nginx`       |
| `gateway-api` | `HTTPRoute` (`gateway.networking.k8s.io/v1`) | an existing `Gateway`, by name/namespace | `zarf package create . -f gateway-api` |
| `istio`       | `VirtualService` (`networking.istio.io/v1`)  | an Istio `Gateway`, by `ns/name`         | `zarf package create . -f istio`       |

Zarf appends the flavor to the tarball name, so all three coexist in one directory: `zarf-package-app-registry-amd64-1.0.0-istio.tar.zst`.

## Why there is a proxy in every flavor

Istio and Gateway API route perfectly well on their own, so the nginx pod is not here to carry traffic. It is here to refuse writes.

Zarf's registry holds a push user. Expose `/v2/` and you have published a writable registry; a client with the push credentials can replace the image any downstream kubelet is about to pull. The proxy serves `GET` and `HEAD` and answers everything else with 403, which is all a kubelet needs:

```
PUT  /v2/foo/blobs/uploads/  -> 403   (refused at the proxy, never reaches the registry)
POST /v2/foo/blobs/uploads/  -> 403
GET  /v2/                    -> proxied
HEAD /v2/                    -> proxied
GET  /                       -> 404
GET  /healthz                -> 200
```

If you would rather route straight at `zarf-docker-registry.zarf.svc` and accept a writable external surface, give the `registry-proxy` component `only.flavor: nginx` and repoint the two route manifests. One line each.

## Deploy

```bash
zarf package deploy zarf-package-app-registry-amd64-1.0.0-nginx.tar.zst
```

| Variable                 | Flavor             | Meaning                                                |
| ------------------------ | ------------------ | ------------------------------------------------------ |
| `REGISTRY_HOSTNAME`      | all                | the FQDN downstream clusters pull from; prompted       |
| `REGISTRY_INGRESS_CLASS` | nginx              | ingress class of the controller already on the cluster |
| `REGISTRY_TLS_SECRET`    | nginx              | a TLS secret in `zarf-registry-external`               |
| `GATEWAY_NAME`           | gateway-api, istio | the `Gateway` to attach to                             |
| `GATEWAY_NAMESPACE`      | gateway-api, istio | that `Gateway`'s namespace                             |

`GATEWAY_NAME` and `GATEWAY_NAMESPACE` default to empty and are not validated, so the Gateway API and Istio flavors will apply an invalid object if you deploy without setting them. For Gateway API, the `Gateway`'s `allowedRoutes` also has to admit the `zarf-registry-external` namespace, which is a change on the `Gateway` rather than here.

TLS terminates at your router, not at the proxy. The registry speaks plain HTTP by design - `RegistryInfo.KnownPlainHTTP()` returns true for any internal Zarf registry without mTLS, and the chart's `tlsSecretName` value is vestigial, used only to name a service port.

## Afterwards

Point Rancher's `systemDefaultRegistry` at `REGISTRY_HOSTNAME`, and give the clusters Rancher provisions the Zarf pull credentials (`zarf tools get-creds`) plus trust for the certificate your router serves. Rancher can distribute that as `registries.yaml` through the RKE2/K3s cluster spec, which is what makes the arrangement self-contained.

## Two things worth knowing

The pod runs in its own `zarf-registry-external` namespace rather than in `zarf`, because that namespace is agent-ignored: the Zarf agent would not rewrite this pod's image reference to the Zarf registry, and it would be pulled from Docker Hub, which does not exist in the air gap.

nginx resolves its upstream once at startup, so the proxy needs a restart if the Zarf registry Service is ever recreated - after a re-init, for instance. Marked with a `shortcut:` comment in the ConfigMap. Deferring resolution to request time needs a `resolver` directive with a literal cluster-DNS address, and `resolver local=on` is **not** supported by nginx 1.31.3 - it fails `nginx -t` outright, so that route crashloops rather than degrades.

## What it requires

[`docs/prerequisites.yaml`](docs/prerequisites.yaml) states the `init` package, because that is what deploys the registry this fronts. What it cannot state is the router each flavor needs: `requires` permits only `kubernetes`, `fluxcd`, `storage` and `packages`, and CRDs are requirable only under `fluxcd`. Worse, `documentation:` is package-level, so all three flavors ship the same document. Check the router by hand.
