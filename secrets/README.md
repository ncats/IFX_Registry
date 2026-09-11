# Local deployment secrets

Place the untracked `aws_ifx_registry.yaml` credential file in this directory
before starting Docker Compose. The file uses the existing IFX assume-role
shape and is mounted read-only into the container.

Place the authorized CURE ID API credential in an untracked `cure_id.yaml`:

```yaml
type: cure_api_key
api_key: replace-with-the-authorized-key
```

Docker Compose mounts it read-only. The Registry sends it only in the
`X-API-Key` request header and never records it in snapshot metadata.
