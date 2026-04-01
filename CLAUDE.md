# corun-ai AI Guidelines

Collaborative AI runner — a harness (scheduler + supervisor) for AI workflows.
First deployment: code documentation for ePIC at epic-devcloud.org/doc/

## Architecture

- Django project at `src/corun_project/`, app at `src/corun_app/`
- PostgreSQL, python-decouple for config, same patterns as swf-remote
- Deployed at `/doc/` on epic-devcloud.org via WSGI subpath
- Gen3 scheduler: JobDefinition → Job → JobStep (phase-based parallelism)

## Conventions

- All models: UUID pk, data JSONField, created_at/modified_at
- In-table versioning: group_id + version, is_current flag
- python-decouple for all config, env prefix CORUN_
- Zero silent failures: every error logged, every except block has traceback
