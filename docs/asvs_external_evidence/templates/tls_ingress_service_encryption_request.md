# ASVS External Evidence Request: TLS, Ingress, Service Encryption

Owner: Ingress / load balancer / platform / network / service-mesh owner

Related ASVS IDs:

- `v5.0.0-12.1.1`
- `v5.0.0-12.1.2`
- `v5.0.0-12.2.1`
- `v5.0.0-12.2.2`
- `v5.0.0-12.3.1`
- `v5.0.0-12.3.2`
- `v5.0.0-12.3.3`
- `v5.0.0-12.3.4`

## Required Attachments

- Public TLS scan for the production vchat hostname proving TLS 1.2/1.3 only.
- Cipher evidence proving recommended cipher suites and preference order.
- Public endpoint evidence proving HTTPS is used and no insecure fallback is
  exposed.
- Publicly trusted certificate chain evidence for the external hostname.
- Service-to-service encryption evidence for PostgreSQL, Redis, LDAP/AD,
  monitoring, registry, ingress-to-pod path, and external API links.
- TLS client certificate validation/trust evidence for TLS clients.
- Internal HTTP service encryption policy, for example service mesh mTLS or
  equivalent.
- Internal CA or pinned/self-signed certificate trust policy where private CAs
  are used.

## Suggested Commands

```bash
testssl.sh --fast --warnings batch https://<production-vchat-host>/
kubectl -n vchat get ingress,svc,networkpolicy,deploy -o yaml
kubectl get peerauthentication,destinationrule,authorizationpolicy -A -o yaml
```

Use equivalent commands if the environment is not Kubernetes or does not use
Istio/service mesh resources.

## Owner Statement

```text
Owner:
Production hostname:
TLS scan date:
Ingress/load balancer:
TLS versions enabled:
Cipher policy:
Certificate issuer/chain:
HTTP fallback exposed: yes/no
Internal encryption mechanism:
Private CA / trust policy:
Exceptions / risk acceptance:
```
