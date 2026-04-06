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


def start_generation(prompt, job_def=None, triggered_by=None):
    """Create a queued Job row. The worker daemon picks it up.

    `triggered_by` is the user who initiated this run — same as
    prompt.submitted_by for original generation, but different for
    reruns where the rerunning user owns the new run while the prompt
    keeps its original author.

    Returns the Job immediately. The web app never runs claude -p.
    """
    if job_def is None:
        job_def = get_or_create_default_def()

    sp_group_id = job_def.data.get('system_prompt_group_id')
    sp_version = None
    if sp_group_id:
        sp = SystemPrompt.objects.filter(group_id=sp_group_id, is_current=True).first()
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
