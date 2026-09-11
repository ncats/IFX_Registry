# Local deployment secrets

Place the untracked `aws_ifx_registry.yaml` credential file in this directory
before starting Docker Compose. The file uses the existing IFX assume-role
shape and is mounted read-only into the container.
