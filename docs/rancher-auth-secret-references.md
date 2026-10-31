# GitOps for Rancher auth, and the secret that looks unreferenceable

Rancher's auth providers live in `management.cattle.io/v3 AuthConfig`, one cluster-scoped object per provider. The object is a good GitOps target - the CRD is schemaless, so plain manifests work - except for one field. Keycloak OIDC has `clientSecret`; LDAP and Active Directory have `serviceAccountPassword`. Both are plain strings. There is no `secretKeyRef`, no `valueFrom`, nothing. The obvious reading is that automating Rancher's auth config means committing the password.

It does not. Rancher has a secret-reference form of its own, it is undocumented, and it is the form Rancher itself writes.

## What Rancher does when you click Save

Configure a provider in the UI and Rancher does not leave the password in the AuthConfig. `common.CreateOrUpdateSecrets` in [`pkg/auth/providers/common/password.go`](https://github.com/rancher/rancher/blob/main/pkg/auth/providers/common/password.go) writes a `Secret` in `cattle-global-data`, names it `<lowercased authconfig type>-<lowercased field>`, stores the value under a single key `<lowercased field>`, and replaces the field in the AuthConfig with `namespace:name`:

```go
name := fmt.Sprintf("%s-%s", authType, field)
secret := &v1.Secret{
    ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: SecretsNamespace},
    StringData: map[string]string{field: secretInfo},
}
return NameForSecret(secret) // "cattle-global-data:keycloakoidcconfig-clientsecret"
```

On the read side, `ReadFromSecret` checks for the `cattle-global-data` prefix, splits on the colon, and reads the key back out. Anything without that prefix is returned as-is, which is how an inline password still works.

So the field is a tagged union: a literal password, or a reference. Writing the reference ourselves is not a workaround bolted on from outside - it is the post-migration state Rancher converges on anyway. Two consequences:

- **The plaintext never enters the manifest.** The Secret is an ordinary `Secret`, so SOPS, sealed-secrets, External Secrets or a `kubectl create secret` all apply to it normally.
- **The object stops drifting.** The secretmigrator has nothing left to rewrite, so the AuthConfig you commit is the AuthConfig on the cluster. Committing an inline password instead means Rancher rewrites the field on every reconcile and Flux writes it back, forever.

## The names, which are derived rather than chosen

| Provider         | AuthConfig name   | `type`                  | Field                    | Secret in `cattle-global-data`                 | Data key                 |
| ---------------- | ----------------- | ----------------------- | ------------------------ | ---------------------------------------------- | ------------------------ |
| Keycloak OIDC    | `keycloakoidc`    | `keyCloakOIDCConfig`    | `clientSecret`           | `keycloakoidcconfig-clientsecret`              | `clientsecret`           |
| Generic OIDC     | `genericoidc`     | `genericOIDCConfig`     | `clientSecret`           | `genericoidcconfig-clientsecret`               | `clientsecret`           |
| OpenLDAP         | `openldap`        | `openLdapConfig`        | `serviceAccountPassword` | `openldapconfig-serviceaccountpassword`        | `serviceaccountpassword` |
| Active Directory | `activedirectory` | `activeDirectoryConfig` | `serviceAccountPassword` | `activedirectoryconfig-serviceaccountpassword` | `serviceaccountpassword` |
| FreeIPA          | `freeipa`         | `freeIpaConfig`         | `serviceAccountPassword` | `freeipaconfig-serviceaccountpassword`         | `serviceaccountpassword` |

Only the first, third and fourth rows ship as packages here; the rest follow the same rule, which is `strings.ToLower(type) + "-" + strings.ToLower(field)`.

**Use the derived name.** An arbitrary name is read back correctly, but the secretmigrator writes to the derived one, so a custom name produces intermittent login failures that look like nothing in particular - [rancher/rancher#56110](https://github.com/rancher/rancher/issues/56110) is that bug, reported as `invalid_request` in the Rancher log.

## What this looks like as a manifest

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: keycloakoidcconfig-clientsecret
  namespace: cattle-global-data
type: Opaque
stringData:
  clientsecret: "###ZARF_VAR_OIDC_CLIENT_SECRET###"
---
apiVersion: management.cattle.io/v3
kind: AuthConfig
metadata:
  name: keycloakoidc
  annotations:
    management.cattle.io/auth-provider-cleanup: unlocked
type: keyCloakOIDCConfig
enabled: true
clientSecret: cattle-global-data:keycloakoidcconfig-clientsecret
clientId: rancher
issuer: https://keycloak.example.com/realms/example
rancherUrl: https://rancher.example.com/verify-auth
accessMode: unrestricted
```

The reference is a constant, not a variable. That is the point: the AuthConfig is identical whether the Secret came from this package or from somewhere else entirely.

## The packages

Three, one per provider, because an auth provider's lifecycle has nothing to do with the other two:

| Package                            | Provider         | Deploys                                   |
| ---------------------------------- | ---------------- | ----------------------------------------- |
| `app-rancher-auth-keycloak`        | Keycloak OIDC    | `keycloakoidc` AuthConfig + its Secret    |
| `app-rancher-auth-openldap`        | OpenLDAP         | `openldap` AuthConfig + its Secret        |
| `app-rancher-auth-activedirectory` | Active Directory | `activedirectory` AuthConfig + its Secret |

Each has the same two components. `authconfig` is required; `secret` is not, and deselecting it is how a Secret from SOPS, sealed-secrets, External Secrets or Ansible gets used instead:

```bash
# Secret from this package, value passed at deploy time
zarf package create app-rancher-auth-keycloak --confirm
zarf package deploy zarf-package-app-rancher-auth-keycloak-amd64-1.0.0.tar.zst \
  --set OIDC_CLIENT_SECRET=<secret>

# Secret from somewhere else
kubectl -n cattle-global-data create secret generic keycloakoidcconfig-clientsecret \
  --from-literal=clientsecret=<secret>
zarf package deploy zarf-package-app-rancher-auth-keycloak-amd64-1.0.0.tar.zst \
  --components authconfig
```

Both paths produce the same AuthConfig. Verify with the grep that matters:

```bash
kubectl get authconfig keycloakoidc -o yaml | grep clientSecret
#   clientSecret: cattle-global-data:keycloakoidcconfig-clientsecret
```

Log in through the provider, then run it again. Unchanged is the proof there is no drift.

## Auth servers behind an internal CA

Air-gapped Keycloak and air-gapped LDAP are almost always behind an internal CA, and the three providers do not agree on how to trust one. The field that looks like the answer - `certificate`, present on all of them - means two different things.

| Provider         | `certificate` field                         | Appended to the trust pool?        | Route that works                     |
| ---------------- | ------------------------------------------- | ---------------------------------- | ------------------------------------ |
| OpenLDAP         | a CA PEM                                    | yes, on top of the system pool     | the field, or `additionalTrustedCAs` |
| Active Directory | a CA PEM                                    | yes, on top of the system pool     | the field, or `additionalTrustedCAs` |
| Keycloak OIDC    | a **client** certificate, paired with a key | only when `privateKey` is also set | `additionalTrustedCAs` only          |

### LDAP and Active Directory: additive

`NewCAPool` starts from the pod's system pool and appends the field, so the two routes compose rather than compete:

```go
func NewCAPool(cert string) (*x509.CertPool, error) {
    pool, err := x509.SystemCertPool()
    if err != nil {
        return nil, err
    }
    pool.AppendCertsFromPEM([]byte(cert))
    return pool, nil
}
```

Either put the CA PEM in the AuthConfig as a block scalar, or trust it once in the Rancher pod. Note what is *not* checked: `AppendCertsFromPEM` returns a bool saying whether it parsed anything, and that return value is discarded. A truncated chain, a DER file, or a PEM whose indentation got mangled on the way into YAML appends nothing at all, and the failure is a generic `x509: certificate signed by unknown authority` - indistinguishable from having configured no CA.

The dialer also pins the hostname:

```go
tlsConfig = &tls.Config{RootCAs: caPool, InsecureSkipVerify: false, ServerName: server}
```

`ServerName` comes from the `servers` entry verbatim, and `InsecureSkipVerify` is hardcoded `false`. So the server certificate's SAN has to match exactly what you put in `servers` - an IP there needs an IP SAN, a short hostname needs that short hostname - and there is no flag to turn verification off.

### Keycloak OIDC: the field is a trap

`getHTTPClient` in `oidc_client.go` reads `certificate` only in the branch where `privateKey` is also non-empty, and that branch parses the pair with `tls.X509KeyPair` as a **client** certificate for mTLS:

```go
pool, err := x509.SystemCertPool()
if certificate != "" && key != "" {
    certs, err := getClientCertificates(certificate, key)   // tls.X509KeyPair
    pool.AppendCertsFromPEM([]byte(certificate))
    ...
}
transport := http.DefaultTransport.(*http.Transport).Clone()
transport.TLSClientConfig.RootCAs = pool
```

A CA certificate on its own is therefore **ignored outright** - no error, no log line, just the system pool and an unknown-authority failure at login. A CA certificate paired with an unrelated key is worse, because `tls.X509KeyPair` fails and the login errors out entirely.

### The route that covers everything

Trust the internal CA in the Rancher pod, once, in `app-rancher`:

```yaml
additionalTrustedCAs: true
```

```bash
kubectl -n cattle-system create secret generic tls-ca-additional \
  --from-file=ca-additional.pem=/path/to/internal-ca.crt
```

The key must be exactly `ca-additional.pem`; the chart mounts it by `subPath`, so a differently-named key mounts nothing. It lands at `/etc/pki/trust/anchors/ca-additional.pem`, the trust-anchor directory the container's `update-ca-certificates` reads at startup, which is what `x509.SystemCertPool()` then returns - and that pool is the base for all three providers plus everything else Rancher dials. **Restart the Rancher pods** after creating or rotating the secret: the mount updates live, the imported trust store does not.

This is a prerequisite the `PackagePrerequisites` documents cannot express - `requires` permits only `kubernetes`, `fluxcd`, `storage` and `packages` - so it lives in prose in each package's description, the same way `app-registry`'s router requirement does.

### Not `privateCA`

`privateCA: true` is a different secret for a different job, and the names are close enough to burn an afternoon:

| Value                  | Secret              | Key                 | Mounted at                     | What it is for                                                      |
| ---------------------- | ------------------- | ------------------- | ------------------------------ | ------------------------------------------------------------------- |
| `additionalTrustedCAs` | `tls-ca-additional` | `ca-additional.pem` | `/etc/pki/trust/anchors/`      | CAs Rancher itself must trust on outbound TLS - auth servers        |
| `privateCA`            | `tls-ca`            | `cacerts.pem`       | `/etc/rancher/ssl/cacerts.pem` | the chain Rancher *serves to agents* so they trust its ingress cert |

`privateCA` does nothing for auth. If your Rancher ingress certificate and your Keycloak certificate come from the same internal CA you will want both, for the two different reasons.

### mTLS, if you actually want it

Setting `certificate` and `privateKey` together on an OIDC config is the supported way to present a client certificate to the provider. `privateKey` is a migrated field like the client secret, so it gets its own reference:

```yaml
privateKey: cattle-global-data:keycloakoidcconfig-privatekey
```

with the Secret keyed `privatekey`. `hack/check-auth-secret-refs.py` already knows about this field, so it is checked the same way if you add it.

## Checking the names before you ship

A typo in a secret name or a data key does not fail the deploy. It fails the first login attempt, in an air gap, with `invalid_request` in the Rancher log and nothing else to go on. `hack/check-auth-secret-refs.py` asserts the three things `ReadFromSecret` needs, offline, across all three packages:

```bash
./hack/check-auth-secret-refs.py
# ok: 3 packages, 3 AuthConfig manifests, all references resolve
```

That the reference matches the derived name, that the Secret exists in the same package, that it carries exactly one key, and that the key is the lowercased field name. It also substitutes each package's own variable defaults and re-parses, so an unquoted `###ZARF_VAR_...###` that renders to invalid YAML - `port:` and `tls:` are both unquoted so they come out as an integer and a boolean - fails here rather than at deploy.

## Four things that will cost you time

**The Secret must have exactly one key, for OIDC.** `oidc_provider.go` reads the client secret with `ReadFromSecretData` and then loops the entire data map, taking the last value it sees, instead of looking the key up:

```go
for _, v := range data {
    storedOidcConfig.ClientSecret = string(v)
}
```

A second key makes the client secret nondeterministic. The LDAP path does look the key up by name, so there the spelling has to be exact instead: `serviceaccountpassword`, all lowercase. `serviceAccountPassword` is read as absent, and the bind fails with an empty password.

**`enabled: false` in git is not an off switch.** `mgmt-auth-config-controller` writes `management.cattle.io/auth-provider-cleanup` onto every AuthConfig - `unlocked` when enabled, `rancher-locked` when not. These manifests pre-set `unlocked` so the controller has nothing to write and there is no drift, but that is also the value that arms cleanup: flipping `enabled` to `false` while it says `unlocked` makes Rancher reset the config and clean up that provider's users and tokens. Set the annotation to `user-locked` first, then disable.

**`accessMode` gates login.** `userMGR.CheckAccess` is what decides, and the packages ship `unrestricted`. `restricted` and `required` need `allowedPrincipalIds` entries shaped `<provider>_group://<id>` or `<provider>_user://<id>`; switch with the list empty and nobody can log in, including you.

**Rancher 2.15 changed which AuthConfigs exist up front.** `deleteEmptyDisabledAuthConfigs` in `pkg/auth/data/authconfig_data.go` now pre-creates only `local` and deletes any AuthConfig that is both disabled and carries no provider fields. So these manifests create their objects cleanly, and no `--adopt-existing-resources` is needed - unlike older Rancher, where every provider's AuthConfig existed from startup. The flip side: a disabled AuthConfig shipped from git survives only because it has provider fields on it. And if a provider was already configured through the UI, its AuthConfig does exist, is not Helm-owned, and the deploy needs `--adopt-existing-resources` after all.

## What is not solved

The value still reaches the cluster somehow. With the `secret` component that is `--set` on the deploy host, so it lands in shell history; `sensitive: true` keeps it out of Zarf's logs, not out of `history`. With the component deselected it is your GitOps repo's problem, which is the right place for it.
