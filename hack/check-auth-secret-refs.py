#!/usr/bin/env python3
"""Check that every Rancher secret reference in the app-rancher-auth-* packages resolves.

A typo in a secret name or a data key does not fail the deploy. It fails the first login
attempt, in an air gap, with `invalid_request` in the Rancher log and nothing else to go
on. This asserts the three things Rancher's ReadFromSecret needs, offline:

  - the `cattle-global-data:<name>` reference points at a Secret the same package ships
  - that Secret carries exactly one key, because the OIDC read path iterates the whole
    data map instead of looking the key up
  - the key is the lowercased field name, which is what ReadFromSecret looks for

It also substitutes the package's own variable defaults and re-parses, so an unquoted
`###ZARF_VAR_...###` that renders to invalid YAML fails here rather than at deploy.

Exits non-zero on the first failure. Run it before `zarf package create`.
"""

import pathlib
import re
import sys

import yaml

NAMESPACE = "cattle-global-data"

# The AuthConfig fields Rancher stores in a Secret, and the key it stores them under.
SECRET_FIELDS = {
    "clientSecret": "clientsecret",
    "serviceAccountPassword": "serviceaccountpassword",
    "privateKey": "privatekey",
}

REPO = pathlib.Path(__file__).resolve().parent.parent

VARIABLE = re.compile(r"###ZARF_VAR_([A-Z0-9_]+)###")


def defaults(package_dir):
    """The package's own variable defaults, which is what Zarf templates with."""
    package = yaml.safe_load((package_dir / "zarf.yaml").read_text())
    # A placeholder for the empty defaults, so an unquoted field is still exercised.
    return {v["name"]: v.get("default") or "x" for v in package["variables"]}


def load(path):
    """Parse a manifest with its variables substituted, as Zarf will render it."""
    values = defaults(path.parent.parent)

    def substitute(match):
        name = match.group(1)
        if name not in values:
            sys.exit(f"{path.name}: ###ZARF_VAR_{name}### is not declared in zarf.yaml")
        return values[name]

    text = VARIABLE.sub(substitute, path.read_text())
    try:
        return [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as err:
        sys.exit(f"{path.name}: does not parse once templated: {err}")


def check(package_dir, errors):
    """Check one package. Secrets only resolve within it, since it ships on its own."""
    docs = [(p, d) for p in sorted((package_dir / "manifests").glob("*.yaml")) for d in load(p)]
    secrets = {(d["metadata"]["namespace"], d["metadata"]["name"]): (p, d) for p, d in docs if d["kind"] == "Secret"}
    configs = [(p, d) for p, d in docs if d["kind"] == "AuthConfig"]

    if not configs:
        sys.exit(f"no AuthConfig manifest found in {package_dir}")

    for path, config in configs:
        for field, key in SECRET_FIELDS.items():
            value = config.get(field)
            if not value or not value.startswith(f"{NAMESPACE}:"):
                continue

            # Rancher derives the name as <lowercased type>-<lowercased field>.
            expected = f"{config['type'].lower()}-{key}"
            namespace, _, name = value.partition(":")
            where = f"{package_dir.name}/{path.name}: {field}"

            if name != expected:
                errors.append(f"{where} references {name!r}, but the secretmigrator writes {expected!r}")

            target = secrets.get((namespace, name))
            if target is None:
                errors.append(f"{where} references {namespace}/{name}, which this package does not create")
                continue

            secret_path, secret = target
            data = {**secret.get("stringData", {}), **secret.get("data", {})}
            if list(data) != [key]:
                errors.append(
                    f"{package_dir.name}/{secret_path.name}: keys are {sorted(data)}, expected exactly ['{key}']"
                )

    return len(configs)


def main():
    packages = sorted(REPO.glob("app-rancher-auth-*"))
    if not packages:
        sys.exit(f"no app-rancher-auth-* packages found under {REPO}")

    errors = []
    checked = sum(check(p, errors) for p in packages)

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        sys.exit(1)
    print(f"ok: {len(packages)} packages, {checked} AuthConfig manifests, all references resolve")


if __name__ == "__main__":
    main()
