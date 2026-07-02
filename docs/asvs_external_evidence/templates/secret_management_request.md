# ASVS External Evidence Request: Secret Management

Owner: Secret management / Kubernetes platform owner

Related ASVS IDs:

- `v5.0.0-13.3.1`
- `v5.0.0-13.3.2`

## Required Attachments

- Kubernetes Secret, External Secrets, Vault, Sealed Secrets, or equivalent
  configuration proving runtime secrets are not stored in source code or image
  artifacts.
- Secret lifecycle policy: creation, rotation, revocation, destruction,
  emergency rotation.
- RBAC/service-account policy proving least-privilege access to vchat secrets.
- Evidence that only intended pods/service accounts can read `vchat-secret` or
  the equivalent external secret.

## Suggested Commands

```bash
kubectl -n vchat get deploy,sa,role,rolebinding -o yaml
kubectl -n vchat get externalsecret,sealedsecret,secretstore,clustersecretstore -o yaml
kubectl -n vchat auth can-i get secrets --as=system:serviceaccount:vchat:<service-account-name>
```

## Owner Statement

```text
Owner:
Secret backend:
vchat secret object/reference:
Service account(s) with read access:
Rotation policy:
Last rotation / creation date:
Emergency revocation procedure:
Exceptions / risk acceptance:
```
