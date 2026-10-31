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
