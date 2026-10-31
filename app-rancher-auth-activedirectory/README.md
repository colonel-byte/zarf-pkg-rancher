# app-rancher-auth-activedirectory

Active Directory as a Rancher auth provider, as two manifests, with the bind password out of the manifest.

`AuthConfig` has no `secretKeyRef` - `serviceAccountPassword` is a plain string - which reads like the plaintext has to live in git. It does not. Rancher has its own secret-reference form, and this package writes it:

```yaml
serviceAccountPassword: cattle-global-data:activedirectoryconfig-serviceaccountpassword
```

The long explanation is in [`docs/rancher-auth-secret-references.md`](../docs/rancher-auth-secret-references.md). The short one: `common.ReadFromSecret` sees the `cattle-global-data` prefix, splits on the colon, and reads the `serviceaccountpassword` key out of that Secret. It is also the state Rancher's own secretmigrator converges on after a UI save, so **nothing rewrites this field** and there is no drift for Zarf or Flux to fight.

## Two components

| Component    | Required | What it is                                                                 |
| ------------ | -------- | -------------------------------------------------------------------------- |
| `secret`     | no       | `cattle-global-data/activedirectoryconfig-serviceaccountpassword`, one key |
| `authconfig` | yes      | the `activedirectory` AuthConfig, enabled, carrying the reference          |

Deselect `secret` and the bind password comes from wherever you want - `kubectl create secret`, SOPS, sealed-secrets, Ansible. The AuthConfig is byte-identical either way, because the reference it carries is a constant rather than a variable.

## Deploy

```bash
zarf package deploy zarf-package-app-rancher-auth-activedirectory-amd64-1.0.0.tar.zst \
  --set AD_SERVICE_ACCOUNT_PASSWORD=<bind password>
```

| Variable                      | Prompted | Meaning                                               |
| ----------------------------- | -------- | ----------------------------------------------------- |
| `AD_SERVERS`                  | yes      | domain controller hostname or IP                      |
| `AD_SERVICE_ACCOUNT_USERNAME` | yes      | UPN or DN of the bind user, e.g. `bind@example.com`   |
| `AD_USER_SEARCH_BASE`         | yes      | base DN to search users under                         |
| `AD_PORT`                     | no       | default `389`                                         |
| `AD_TLS`                      | no       | `true` for LDAPS on that port; default `false`        |
| `AD_GROUP_SEARCH_BASE`        | no       | Rancher falls back to the user search base when empty |
| `AD_DEFAULT_LOGIN_DOMAIN`     | no       | NetBIOS domain prepended to bare usernames at login   |
| `AD_SERVICE_ACCOUNT_PASSWORD` | no       | `secret` component only; pass with `--set`            |

`AD_SERVICE_ACCOUNT_PASSWORD` is deliberately not prompted. Zarf prompts per package, not per selected component, so prompting would demand a password that an out-of-band deploy does not need.

Everything else is a literal in `manifests/authconfig.yaml` set to Rancher's Active Directory defaults - `sAMAccountName` as the login attribute, `userAccountControl` with `disabledStatusBitmask: 2`, which is how AD marks an account disabled. Edit the file for a forest that differs; the [Active Directory config reference](https://ranchermanager.docs.rancher.com/how-to-guides/new-user-guides/authentication-permissions-and-global-configuration/configure-active-directory) is the field-by-field source.

`servers` holds one entry. A Zarf variable substitutes a string, not a YAML list, so more than one domain controller means adding entries by hand - marked with a `shortcut:` comment in the manifest.

If `AD_TLS` is `true`, set `certificate` in the manifest to the PEM chain of the CA that issued the DC's certificate; Rancher will not fall back to the system trust store.

## After deploying

```bash
kubectl get authconfig activedirectory -o yaml | grep serviceAccountPassword
#   serviceAccountPassword: cattle-global-data:activedirectoryconfig-serviceaccountpassword
```

Then log in with a domain account in the Rancher UI, and run that `grep` again. Unchanged is the proof the object is stable under GitOps.

`accessMode` ships as `unrestricted`. For `restricted`, add `allowedPrincipalIds` entries shaped `activedirectory_group://<dn>` or `activedirectory_user://<dn>` - and add at least one before you switch, or nobody can log in.

## Gotchas

**The data key spelling is exact.** `ReadFromSecret` looks up `serviceaccountpassword`, all lowercase. `serviceAccountPassword` is read as absent, and the bind then fails with an empty password. `hack/check-auth-secret-refs.py` asserts this.

**Do not set `enabled: false` in git to turn the provider off.** The AuthConfig carries `management.cattle.io/auth-provider-cleanup: unlocked` - pre-set so the controller does not write it and cause drift - and disabling while unlocked makes Rancher reset the config and clean up this provider's users and tokens. Set the annotation to `user-locked` first.

**If Active Directory was already configured through the UI**, the AuthConfig exists and Helm will not adopt it. Deploy with `--adopt-existing-resources`.
