"""Job submission — creates Job rows for the worker to pick up."""

import uuid

from corun_app.models import Job, JobDefinition, SystemPrompt


def get_or_create_default_def():
    """Get or create the default codoc job definition with system prompt."""
    job_def = JobDefinition.objects.filter(name='codoc-generate').first()
    if job_def:
        return job_def

    sp_group = uuid.uuid4()
    sp = SystemPrompt.objects.create(
        group_id=sp_group,
        version=1,
        is_current=True,
        name='codoc-default',
        content=DEFAULT_SYSTEM_PROMPT,
        data={'description': 'Default system prompt for ePIC code documentation generation'},
    )

    job_def = JobDefinition.objects.create(
        name='codoc-generate',
        description='Generate documentation page from prompt using claude -p with MCP tools',
        data={
            'system_prompt_group_id': str(sp.group_id),
            'model': 'sonnet',
            'effort': 'high',
            'mcp_tools': ['lxr', 'github'],
        },
    )
    return job_def


def start_generation(prompt, job_def=None, triggered_by=None, system_prompt_version=None):
    """Create a queued Job row. The worker daemon picks it up.

    `triggered_by` is the user who initiated this run — same as
    prompt.submitted_by for original generation, but different for
    reruns where the rerunning user owns the new run while the prompt
    keeps its original author.

    `system_prompt_version` pins a specific sysprompt version for this
    run (used by reruns to reproduce the original or to deliberately
    pick the latest after a drift confirmation). None = use whatever
    is currently is_current at submission time. The pin is stored in
    job.data['system_prompt_version'] and the worker honors it.

    Returns the Job immediately. The web app never runs claude -p.
    """
    if job_def is None:
        job_def = get_or_create_default_def()

    sp_group_id = job_def.data.get('system_prompt_group_id')
    sp_version = None
    if sp_group_id:
        if system_prompt_version is not None:
            sp = SystemPrompt.objects.filter(
                group_id=sp_group_id, version=system_prompt_version,
            ).first()
        else:
            sp = SystemPrompt.objects.filter(
                group_id=sp_group_id, is_current=True,
            ).first()
        if sp:
            sp_version = sp.version

    job = Job.objects.create(
        definition=job_def,
        prompt=prompt,
        triggered_by=triggered_by,
        status='queued',
        data={
            'system_prompt_version': sp_version,
            'definition_name': job_def.name,
        },
    )

    prompt.status = 'queued'
    prompt.save(update_fields=['status', 'modified_at'])

    return job


def get_or_create_snippet_review_def():
    """Get or create the codoc-snippet-review job definition with its system prompt."""
    job_def = JobDefinition.objects.filter(name='codoc-snippet-review').first()
    if job_def:
        return job_def

    sp_group = uuid.uuid4()
    sp = SystemPrompt.objects.create(
        group_id=sp_group,
        version=1,
        is_current=True,
        name='codoc-snippet-review',
        content=DEFAULT_SNIPPET_REVIEW_SYSTEM_PROMPT,
        data={'description': 'System prompt for ePIC snippet review'},
    )

    job_def = JobDefinition.objects.create(
        name='codoc-snippet-review',
        description='Review an eic/snippets file against current ePIC practices using claude -p with MCP tools',
        data={
            'system_prompt_group_id': str(sp.group_id),
            'model': 'sonnet',
            'effort': 'high',
            'mcp_tools': ['lxr', 'github'],
        },
    )
    return job_def


DEFAULT_SNIPPET_REVIEW_SYSTEM_PROMPT = """\
You are a code reviewer for the ePIC experiment at the Electron-Ion Collider (EIC). \
You evaluate code snippets from the eic/snippets repository against current best \
practices for EIC software development.

Your review must cover:
1. **Correctness against current frameworks** — does the snippet work with the \
current versions of DD4hep, ACTS, EICrecon, ePIC reconstruction software, and \
other EIC frameworks in active use?
2. **What works well** — patterns and idioms that are exemplary and should be \
highlighted as good examples for other developers.
3. **Issues and risks** — deprecated APIs, outdated patterns, incorrect assumptions, \
or code that is likely to break or produce wrong results with current software.
4. **Recommended updates** — concrete, actionable suggestions with corrected code \
where needed.

You have access to MCP tools for verifying current implementations:

**LXR Code Browser** (eic-code-browser.sdcc.bnl.gov/lxr):
- lxr_ident: Find where a symbol (class, function, variable) is defined and used
- lxr_search: Ripgrep-powered text/regex search across all 55+ EIC repositories
- lxr_source: Read source files with line numbers
- lxr_list: Browse directory structure

USE THESE TOOLS. Every claim about current practice must be grounded in code you \
have looked up — do not rely on memory alone. Check how the relevant APIs are \
actually used in current EIC repositories before making any assessment.

**Output format:**
- **Summary**: one-paragraph overview of the snippet and your overall verdict
- **What Works**: bullet list of good patterns worth keeping/highlighting
- **Issues Found**: numbered list of problems, each with severity (critical/major/minor) \
and explanation
- **Recommended Updates**: diff-style or annotated code showing what to change

Include LXR links for every class, function, or file you reference: \
https://eic-code-browser.sdcc.bnl.gov/lxr/source/<path>#<line>
"""


DEFAULT_SNIPPET_REVIEW_PROMPT_TEMPLATE = """\
Please review this code snippet from the eic/snippets repository.

File: {path}
GitHub: {gh_url}

```
{content}
```

Evaluate this snippet against current ePIC/EIC software practices:
- Does it work correctly with the current versions of DD4hep, ACTS, EICrecon, \
and other active EIC frameworks?
- Are there deprecated APIs, outdated patterns, or potential incompatibilities?
- What does the snippet demonstrate well — what should be highlighted as a good \
example?
- What needs to be updated, fixed, or clarified?

Use the LXR code browser to verify current implementations and API usage before \
forming your assessment.
"""


DEFAULT_SYSTEM_PROMPT = """\
You are a technical documentation writer for the ePIC experiment at the \
Electron-Ion Collider (EIC). You produce clear, well-structured documentation \
about ePIC software, algorithms, and systems.

You have access to MCP tools for browsing the actual EIC codebase:

**LXR Code Browser** (eic-code-browser.sdcc.bnl.gov/lxr):
- lxr_ident: Find where a symbol (class, function, variable) is defined and referenced
- lxr_search: Ripgrep-powered text/regex search across all 55+ EIC repositories
- lxr_source: Read source files with line numbers
- lxr_list: Browse directory structure

USE THESE TOOLS. Every documentation page must be grounded in actual code. \
Do not write from memory alone — look up the real implementations.

**Formatting requirements:**
- Write in markdown with headings, code blocks, and lists
- Every class, function, or file you mention MUST include a clickable LXR link: \
https://eic-code-browser.sdcc.bnl.gov/lxr/source/<path>#<line>
- For GitHub references, link to: https://github.com/eic/<repo>/blob/main/<path>
- Include code snippets from actual source files (use lxr_source to read them)
- Be accurate, concise, and useful to physicists and software developers

**Workflow:**
1. Use lxr_ident and lxr_search to find the relevant code
2. Use lxr_source to read key implementations
3. Write documentation grounded in what you found, with links throughout
"""
