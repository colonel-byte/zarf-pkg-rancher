# app-rancher-auth-keycloak

Keycloak OIDC as a Rancher auth provider, as two manifests, with the client secret out of the manifest.

`AuthConfig` has no `secretKeyRef` - `clientSecret` is a plain string - which reads like the plaintext has to live in git. It does not. Rancher has its own secret-reference form, and this package writes it:

```yaml
clientSecret: cattle-global-data:keycloakoidcconfig-clientsecret
```

The long explanation is in [`docs/rancher-auth-secret-references.md`](../docs/rancher-auth-secret-references.md). The short one: `common.ReadFromSecret` sees the `cattle-global-data` prefix, splits on the colon, and reads the `clientsecret` key out of that Secret. It is also the state Rancher's own secretmigrator converges on after a UI save, so **nothing rewrites this field** and there is no drift for Zarf or Flux to fight.

## Two components

| Component    | Required | What it is                                                     |
| ------------ | -------- | -------------------------------------------------------------- |
| `secret`     | no       | `cattle-global-data/keycloakoidcconfig-clientsecret`, one key  |
| `authconfig` | yes      | the `keycloakoidc` AuthConfig, enabled, carrying the reference |

Deselect `secret` and the client secret comes from wherever you want - `kubectl create secret`, SOPS, sealed-secrets, Ansible. The AuthConfig is byte-identical either way, because the reference it carries is a constant rather than a variable.

## Deploy

```bash
zarf package deploy zarf-package-app-rancher-auth-keycloak-amd64-1.0.0.tar.zst \
  --set OIDC_CLIENT_SECRET=<the secret from Keycloak's client credentials tab>
```

| Variable             | Prompted | Meaning                                                              |
| -------------------- | -------- | -------------------------------------------------------------------- |
| `RANCHER_HOSTNAME`   | yes      | FQDN the Rancher ingress answers on; becomes `rancherUrl`            |
| `OIDC_ISSUER`        | yes      | realm issuer URL, e.g. `https://keycloak.example.com/realms/example` |
| `OIDC_CLIENT_ID`     | no       | the Keycloak client ID, default `rancher`                            |
| `OIDC_CLIENT_SECRET` | no       | `secret` component only; pass with `--set`                           |

`OIDC_CLIENT_SECRET` is deliberately not prompted. Zarf prompts per package, not per selected component, so prompting would demand a password that an out-of-band deploy does not need.

The five endpoint fields are built from `OIDC_ISSUER` using Keycloak's standard `/protocol/openid-connect/...` layout, so there is nothing else to set. In Keycloak, the client needs `https://<RANCHER_HOSTNAME>/verify-auth` as a valid redirect URI and a `groups` claim mapper if you want group membership.

## Keycloak behind an internal CA

**The AuthConfig's `certificate` field will not do this.** It looks like a trust field and is not one. `getHTTPClient` in `oidc_client.go` only touches it when `certificate` *and* `privateKey` are both set, and in that branch it loads them as a **client** certificate pair for mTLS:

```go
pool, err := x509.SystemCertPool()
if certificate != "" && key != "" {
    certs, err := getClientCertificates(certificate, key)   // tls.X509KeyPair
    pool.AppendCertsFromPEM([]byte(certificate))
    ...
}
```

A CA PEM on its own, with no `privateKey`, falls past the `if` and is silently ignored - you get the system pool and an `x509: certificate signed by unknown authority` that names nothing. A CA PEM paired with some unrelated key is worse: `tls.X509KeyPair` fails and login errors out.

So the only route is the Rancher pod's own trust store. In `app-rancher`:

```yaml
additionalTrustedCAs: true
```

and a secret in `cattle-system` holding the internal CA, keyed exactly `ca-additional.pem`:

```bash
kubectl -n cattle-system create secret generic tls-ca-additional \
  --from-file=ca-additional.pem=/path/to/internal-ca.crt
```

The chart mounts that at `/etc/pki/trust/anchors/ca-additional.pem`, the trust-anchor directory the container's `update-ca-certificates` reads at startup, so it lands in the system pool that `x509.SystemCertPool()` returns. **Rancher pods must be restarted** after the secret changes - the mount updates, the trust store does not re-import.

Do not reach for `privateCA: true` instead. That is a different secret (`tls-ca`, keyed `cacerts.pem`, mounted at `/etc/rancher/ssl/cacerts.pem`) and a different job: it is the chain Rancher *serves to downstream agents* so they trust Rancher's own ingress certificate. It does nothing for Rancher's outbound TLS to Keycloak. Conflating the two is the usual way an afternoon disappears.

One piece of luck: `FetchAuthURL` does OIDC discovery with a bare `http.Client` and no custom pool at all, but it returns early when `authEndpoint` is already set - which this package always sets. Leave it set.

If you do want mTLS to Keycloak, set both `certificate` and `privateKey`. `privateKey` is a migrated field like the client secret, so it belongs in `cattle-global-data/keycloakoidcconfig-privatekey` under the key `privatekey`, referenced the same way.

## After deploying

```bash
kubectl get authconfig keycloakoidc -o yaml | grep clientSecret
#   clientSecret: cattle-global-data:keycloakoidcconfig-clientsecret
```

Then log in through Keycloak in the Rancher UI, and run that `grep` again. Unchanged is the proof the object is stable under GitOps.

`accessMode` ships as `unrestricted`. For `restricted`, add `allowedPrincipalIds` entries shaped `keycloakoidc_group://<group>` or `keycloakoidc_user://<sub>` - and add at least one before you switch, or nobody can log in.

## Gotchas

**The Secret must have exactly one key.** The OIDC read path (`oidc_provider.go`) iterates the whole data map rather than looking the key up, so a second key makes the client secret nondeterministic. `hack/check-auth-secret-refs.py` asserts this.

**Do not set `enabled: false` in git to turn the provider off.** The AuthConfig carries `management.cattle.io/auth-provider-cleanup: unlocked` - pre-set so the controller does not write it and cause drift - and disabling while unlocked makes Rancher reset the config and clean up this provider's users and tokens. Set the annotation to `user-locked` first.

**If Keycloak was already configured through the UI**, the AuthConfig exists and Helm will not adopt it. Deploy with `--adopt-existing-resources`.
