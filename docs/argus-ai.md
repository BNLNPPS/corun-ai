# argus-ai

argus-ai is the second corun-ai application, sister to codoc-ai. Where codoc-ai
generates documentation, argus-ai **watches**: it runs registered *Probes* that
assess external targets and return natural-language assessments — beginning with
the physics monitoring plots in JLab's Hydra validation browser. Probes are created on the web interface; they can be triggered there or on request from the ePIC PanDA bot via the corun-ai MCP server. The bot relays the assessment back to Mattermost, so experts can ask for a Probe run and get the LLM's judgment in the same channel.

This note records the design and the open questions. 

## Vocabulary

- **corun-ai** — the harness argus-ai runs in: humans assemble inputs and tools
  into hybrid AI/programmatic workflows, a scheduler runs them, and humans and AIs
  curate the results. codoc-ai (collaborative documentation) is its first
  application; argus-ai is the second.
- **argus-ai** — the app (Argus Panoptes, the hundred-eyed watcher). Holds the
  catalog of Probes and the assessment history. Lives at `epic-devcloud.org/argus/`, sister to codoc-ai at `epic-devcloud.org/doc/`.
- **Probe** — the unit: an eye on a target. *"A Probe on a Hydra endpoint"*
  reads as what it does. A Probe is owner + input manifest + prompt (+ optional
  procedural artifacts, cadence, requestor). An expert defines the observation target, the data artifacts at that target, the assessment process, and the prompt driving the LLM part of the assessment.
- **Hydra** — an **external** plotting/validation app from JLab, used for ePIC monitoring/validation, that
  provides Probe *targets*. Hydra is the first target for argus-ai.
- **wrangle-ai** — the execution core corun-ai uses to run Probes: a bounded
  worker pool that wakes on a signal, runs each unit of work to completion, and
  records outcomes durably. Terms used below: `Worker` (one unit of work — a Probe
  run), `Bullpen` (the durable store), `Bell` (the wake signal).

## The Probe spec

A Probe holds N ≥ 1 input manifests, assessed together. Each manifest describes one input:

- `path` / `url` — where the input is fetched.
- `datatype` — `page` | `json` | `text` (see Input surfaces).
- `interpretation` — a per-input hint telling the LLM what the input is and how to
  read it, so one prompt can reason over heterogeneous inputs.

The **prompt is the program over the manifests**: assess each input, and/or collectively
assess and synthesize, and/or assess over a time history using past versions.

A Probe may carry **procedural artifacts**, making each Probe a small harness:

- **extract** — deterministic scripts that pull each input into standard form
  before the model sees it.
- **reason** — the prompt over the normalized data (the LLM).
- **report/standardize** — deterministic template or plotting tools that shape the
  output.

The deterministic-core / LLM-for-judgment split applies per Probe: numbers and
normalization are code, the assessment prose is the model.

A time dimension — sampling frequency / interval — is anticipated; the first round is solely event- and request-triggered.

## Input surfaces

The supported input surfaces are the same for every Probe. The first round supports:

- the page itself, including if desired its images assessed by a multimodal model
- JSON
- text

## Execution

Probes run on a wrangle-ai based executor — a corun enhancement, used first by
argus-ai and available later to codoc-ai. A Probe run is a wrangle `Worker`; the
wrangle core provides the bounded pool, backpressure, in-flight dedup, and
graceful drain. Two seams:

- **Bullpen** — the durable store of Probes and their runs.
- **Bell** — the wake signal. A run is triggered from the web UI, the bot (via
  MCP), or an inbound REST call (e.g. Hydra signalling that a validation is
  available for a target). Auto-running a Probe on every inbound trigger is a
  capability gated by policy — default off, enabled per Probe or per source.

Within a Probe, execution is **scatter / gather**: serial → scatter → gather →
serial, matching the manifest's collective modes — per-input assessments scatter,
synthesis gathers, history comparison gathers over versions. It maps to corun's
`JobStep` phase model. Execution is limited to scatter/gather.

argus-ai supports **parallel Probe execution and in-Probe fan-out**, both drawing
on one shared concurrency budget whose cap is set by policy/ops.

## Results and history

Assessment results use corun's `Page` versioning (`group_id` + `version` +
`is_current`): each run is a new version of the Probe's result page-group, giving
free history of a moving target. Time and benchmark comparison are expressed in
the prompt — a run's inputs are the current target, its reference/benchmark, and
prior version(s). An assessment of "changed but acceptable" is a first-class
result.

## Reporting

On completion argus-ai notifies outward, and a request may register **one or more**
completion endpoints — every registered endpoint receives the outcome:

- **Web UI and corun job-completion callbacks** — the in-app surface.
- **Bot via the MCP server** — targets the reply to a Mattermost channel or DM
  using the submitting job id.
- **Caller-specified REST webhooks** — N endpoints a requester (e.g. Hydra, prod
  tooling) registers per request, each POSTed the outcome.

argus-ai records the requestor for accountability and catalog.

## Security

Probes fetch operator-defined targets from the server. Access is governed by a
minimal denylist of internal and private network ranges; all external targets are
allowed. The posture is essentialist: protective of the host, unobtrusive to
experts.

## App structure and URL topology

corun-ai is the harness and owns the site root, presenting its applications.
codoc-ai is served at `/doc/` and argus-ai at `/argus/`. Shared
functionality and models live in `corun_app` (Page, Prompt, Comment, SystemPrompt, Job /
JobDefinition / JobStep, SiteContent). `argus_app` holds argus-ai's views,
templates, the Probe harness, and the `Probe` and `ProbeRun` models, reusing
`corun_app`'s `Page` for history.

## About pages

codoc-ai and argus-ai each have their own About page. The two cross-reference one
another and link to and describe their shared corun-ai foundation.

## Open questions

- How a Probe specifies its reference/benchmark.
- The Probe catalog and the name resolution the bot uses to map a request to a Probe.
