# 0003: Provider configuration in a file, secrets in the environment

## Status

Accepted

## Context

The gateway needs structured configuration (providers with several settings each,
and aliases that reference them) and secrets (gateway keys and provider keys).
Nested structures are awkward to express in environment variables, while secrets
should not be stored in files that are committed or baked into images.

## Decision

- Providers and aliases are defined in a TOML file, parsed with the standard
  library `tomllib` and validated with Pydantic models that reject unknown keys.
- The file never contains secret values. Each provider names the environment
  variable holding its key in `api_key_env`.
- Gateway settings and gateway keys come from `GATEWAY_*` environment variables via
  pydantic-settings.
- All configuration is validated at startup; any error stops the process.

## Alternatives considered

- **Environment variables only**, with nested JSON values. Hard to read and review,
  and errors are harder to locate.
- **Secrets in the configuration file.** Simpler to deploy, but the file could no
  longer be committed or shared, and secrets would end up in images and backups.
- **YAML.** Needs a third-party parser and has surprising implicit typing; TOML is
  in the standard library.

## Consequences

- The configuration file can be committed, reviewed, and mounted read-only into
  containers.
- Operators manage two places: the file and the environment. A provider whose key
  variable is missing fails at startup rather than on the first request.
- Configuration changes require a restart.
