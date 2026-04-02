"""Code documentation views — the first corun-ai application."""

import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

import markdown as md_lib

from corun_app.models import (
    AppLog, Job, JobDefinition, Page, Prompt, Section,
    SiteContent, SystemPrompt, UserProfile,
)

logger = logging.getLogger(__name__)


# ── Browse (two-panel home) ──────────────────────────────────────────────────

def home(request):
    """Two-panel browse: pages (documents) on left, detail on right."""
    sections = Section.objects.filter(status='active')

    for sec in sections:
        pages = list(sec.pages.filter(
            is_current=True, status='published'
        ).order_by('-created_at'))
        sec.browse_items = pages

    return render(request, 'codoc_app/home.html', {
        'sections': sections,
    })


def _get_prompt_def(prompt):
    """Get the definition for a prompt: from prompt.data, then job, then default."""
    # 1. Explicit definition on the prompt
    def_id = prompt.data.get('definition_id')
    if def_id:
        d = JobDefinition.objects.filter(id=def_id).first()
        if d:
            return d
    # 2. From most recent job
    job = Job.objects.filter(prompt=prompt).order_by('-created_at').first()
    if job:
        return job.definition
    # 3. Default
    return JobDefinition.objects.filter(name='codoc-generate').first()


def prompt_fragment(request, group_id):
    """AJAX fragment: prompt detail for right panel."""
    prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
    pages = Page.objects.filter(prompt__group_id=group_id, is_current=True)
    job_def = _get_prompt_def(prompt)
    definitions = JobDefinition.objects.filter(status='active')
    html = render_to_string('codoc_app/_prompt_fragment.html', {
        'prompt': prompt, 'pages': pages, 'job_def': job_def,
        'definitions': definitions,
    }, request=request)
    return HttpResponse(html)


def page_fragment(request, group_id):
    """AJAX fragment: page detail for right panel."""
    page = get_object_or_404(Page, group_id=group_id, is_current=True)
    # Find the definition used
    job_def = None
    if page.data.get('definition_id'):
        job_def = JobDefinition.objects.filter(id=page.data['definition_id']).first()
    if not job_def:
        job = Job.objects.filter(prompt=page.prompt).order_by('-created_at').first()
        job_def = job.definition if job else None
    html = render_to_string('codoc_app/_page_fragment.html', {
        'page': page, 'job_def': job_def,
    }, request=request)
    return HttpResponse(html)


# ── Inline editor fragment ──────────────────────────────────────────────────

def editor_fragment(request, group_id=None):
    """AJAX fragment: inline editor in right panel. group_id=None for new prompt."""
    sections = Section.objects.filter(status='active')
    definitions = JobDefinition.objects.filter(status='active')
    prompt = None
    if group_id:
        prompt = Prompt.objects.filter(group_id=group_id, is_current=True).first()
    html = render_to_string('codoc_app/_editor_fragment.html', {
        'prompt': prompt, 'sections': sections, 'definitions': definitions,
    }, request=request)
    return HttpResponse(html)


# ── Save/Generate from inline editor ────────────────────────────────────────

@require_POST
@login_required
def save_prompt_api(request):
    """AJAX: save or generate from inline editor. Returns JSON."""
    section_id = request.POST.get('section')
    content = request.POST.get('content', '').strip()
    action = request.POST.get('action', 'save')
    source_group_id = request.POST.get('source_group_id', '')
    definition_id = request.POST.get('definition', '')

    if not content:
        return JsonResponse({'error': 'Prompt content is required.'}, status=400)

    sec = get_object_or_404(Section, id=section_id)

    # Determine if this is a new version of existing prompt or brand new
    if source_group_id:
        # New version of existing prompt
        prev = Prompt.objects.filter(group_id=source_group_id, is_current=True).first()
        if prev:
            prev.is_current = False
            prev.save(update_fields=['is_current', 'modified_at'])
            new_version = prev.version + 1
            group_id = prev.group_id
        else:
            group_id = uuid.uuid4()
            new_version = 1
    else:
        group_id = uuid.uuid4()
        new_version = 1

    prompt = Prompt.objects.create(
        group_id=group_id, version=new_version, is_current=True,
        section=sec, content=content, submitted_by=request.user,
        status='saved',
        data={'definition_id': definition_id} if definition_id else {},
    )

    if action == 'generate':
        from .generate import start_generation
        job_def = None
        if definition_id:
            job_def = JobDefinition.objects.filter(id=definition_id).first()
        job = start_generation(prompt, job_def)
        return JsonResponse({
            'ok': True, 'action': 'generate',
            'prompt_group_id': str(prompt.group_id),
            'job_id': str(job.id),
        })
    else:
        return JsonResponse({
            'ok': True, 'action': 'save',
            'prompt_group_id': str(prompt.group_id),
        })


# ── Generate from existing prompt ───────────────────────────────────────────

@login_required
def generate_from_prompt(request, group_id):
    """Start async generation from an existing prompt."""
    prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
    job_def = _get_prompt_def(prompt)

    from .generate import start_generation
    job = start_generation(prompt, job_def)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'job_id': str(job.id)})
    return redirect('codoc:queue')


# ── Prepare prompt (standalone page, fallback) ──────────────────────────────

@login_required
def prepare_prompt(request):
    """Standalone prepare prompt page."""
    sections = Section.objects.filter(status='active')
    definitions = JobDefinition.objects.filter(status='active')
    from .generate import get_or_create_default_def
    job_def = get_or_create_default_def()
    sysprompt = None
    sp_gid = job_def.data.get('system_prompt_group_id')
    if sp_gid:
        sysprompt = SystemPrompt.objects.filter(group_id=sp_gid, is_current=True).first()

    if request.method == 'POST':
        section_id = request.POST.get('section')
        content = request.POST.get('content', '').strip()
        action = request.POST.get('action', 'save')
        definition_id = request.POST.get('definition', '')

        if not content:
            messages.error(request, 'Prompt content is required.')
            return render(request, 'codoc_app/prepare.html', {
                'sections': sections, 'definitions': definitions,
                'sysprompt': sysprompt, 'job_def': job_def,
            })

        sec = get_object_or_404(Section, id=section_id)
        group_id = uuid.uuid4()
        prompt = Prompt.objects.create(
            group_id=group_id, version=1, is_current=True,
            section=sec, content=content, submitted_by=request.user,
            status='saved',
            data={'definition_id': definition_id} if definition_id else {},
        )

        if action == 'generate':
            from .generate import start_generation
            jd = None
            if definition_id:
                jd = JobDefinition.objects.filter(id=definition_id).first()
            start_generation(prompt, jd)
            return redirect('codoc:queue')
        else:
            messages.success(request, 'Prompt saved.')
            return redirect('codoc:home')

    return render(request, 'codoc_app/prepare.html', {
        'sections': sections, 'definitions': definitions,
        'sysprompt': sysprompt, 'job_def': job_def,
    })


# ── Prompts (two-panel) ────────────────────────────────────────────────────

def prompts_view(request):
    """Two-panel prompt library."""
    prompts = Prompt.objects.filter(
        is_current=True
    ).exclude(status='rejected').select_related('section').order_by('-created_at')
    return render(request, 'codoc_app/prompts.html', {'prompts': prompts})


def prompt_view_frag(request, group_id):
    """AJAX fragment: prompt detail for prompts page right panel."""
    prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
    all_defs = {str(d.id): d.name for d in JobDefinition.objects.all()}
    prompt.def_name = all_defs.get(prompt.data.get('definition_id', ''), '')
    versions = Prompt.objects.filter(group_id=group_id).order_by('-version')
    html = render_to_string('codoc_app/_prompt_view_fragment.html', {
        'prompt': prompt, 'versions': versions,
    }, request=request)
    return HttpResponse(html)


def prompt_edit_frag(request, group_id=None):
    """AJAX fragment: prompt edit form."""
    prompt = None
    if group_id:
        prompt = Prompt.objects.filter(group_id=group_id, is_current=True).first()
    sections = Section.objects.filter(status='active')
    definitions = JobDefinition.objects.filter(status='active')
    html = render_to_string('codoc_app/_prompt_edit_fragment.html', {
        'prompt': prompt, 'sections': sections, 'definitions': definitions,
    }, request=request)
    return HttpResponse(html)


# ── Delete prompt/page ─────────────────────────────────────────────────────

@require_POST
@login_required
def prompt_delete(request, group_id):
    """Delete a prompt and its pages. Owner only."""
    prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
    if prompt.submitted_by != request.user:
        return JsonResponse({'error': 'Not your prompt.'}, status=403)
    # Only delete the prompt versions, not pages or jobs
    Prompt.objects.filter(group_id=group_id).delete()
    return JsonResponse({'ok': True})


@require_POST
@login_required
def page_delete(request, group_id):
    """Delete a page. Owner only."""
    page = get_object_or_404(Page, group_id=group_id, is_current=True)
    if page.prompt.submitted_by != request.user:
        return JsonResponse({'error': 'Not your page.'}, status=403)
    Page.objects.filter(group_id=group_id).delete()
    return JsonResponse({'ok': True})


# ── Detail pages (direct URL access) ────────────────────────────────────────

def section_detail(request, section):
    sec = get_object_or_404(Section, name=section)
    prompts = Prompt.objects.filter(section=sec, is_current=True).exclude(status='rejected')
    pages = Page.objects.filter(section=sec, is_current=True, status='published')
    return render(request, 'codoc_app/section.html', {
        'section': sec, 'prompts': prompts, 'pages': pages,
    })


def prompt_detail(request, group_id):
    prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
    pages = Page.objects.filter(prompt__group_id=group_id, is_current=True)
    return render(request, 'codoc_app/prompt.html', {'prompt': prompt, 'pages': pages})


def page_detail(request, group_id):
    page = get_object_or_404(Page, group_id=group_id, is_current=True)
    return render(request, 'codoc_app/page.html', {'page': page})


# ── Queue (job history) ─────────────────────────────────────────────────────

def queue(request):
    """Job queue: running and completed jobs."""
    jobs = Job.objects.select_related('definition', 'prompt').order_by('-created_at')[:100]
    active = [j for j in jobs if j.status in ('queued', 'running')]
    completed = [j for j in jobs if j.status in ('completed', 'failed', 'cancelled')]
    return render(request, 'codoc_app/queue.html', {
        'active': active, 'completed': completed,
    })


def queue_status_api(request):
    """AJAX: return current queue state + process monitoring for active jobs."""
    import subprocess as sp
    from django.utils import timezone as tz

    jobs = Job.objects.select_related('definition', 'prompt').order_by('-created_at')[:50]

    # Find claude -p processes spawned by the worker
    claude_procs = {}
    try:
        # Get worker PID, then find its children
        worker_ps = sp.run(
            ['pgrep', '-f', 'worker\\.py'], capture_output=True, text=True, timeout=5,
        )
        worker_pids = worker_ps.stdout.strip().splitlines()
        for wpid in worker_pids:
            children = sp.run(
                ['pgrep', '-P', wpid.strip()], capture_output=True, text=True, timeout=5,
            )
            for cpid in children.stdout.strip().splitlines():
                cpid = cpid.strip()
                if not cpid:
                    continue
                try:
                    stat = sp.run(
                        ['ps', '-p', cpid, '-o', 'pid=,pcpu=,pmem=,etime='],
                        capture_output=True, text=True, timeout=5,
                    )
                    parts = stat.stdout.strip().split()
                    if len(parts) >= 4:
                        claude_procs[parts[0]] = {
                            'pid': parts[0],
                            'cpu': parts[1],
                            'mem': parts[2],
                            'time': parts[3],
                        }
                except Exception:
                    pass
    except Exception:
        pass

    # Get IO stats for claude processes
    for pid in list(claude_procs.keys()):
        try:
            with open(f'/proc/{pid}/io', 'r') as f:
                io_data = {}
                for line in f:
                    k, v = line.strip().split(': ')
                    io_data[k] = int(v)
                claude_procs[pid]['read_bytes'] = io_data.get('read_bytes', 0)
                claude_procs[pid]['write_bytes'] = io_data.get('write_bytes', 0)
        except Exception:
            pass

    result = []
    for j in jobs:
        result.append({
            'id': str(j.id),
            'status': j.status,
            'definition': j.definition.name,
            'prompt': j.prompt.content[:80] if j.prompt else j.data.get('prompt_content', '')[:80],
            'created': tz.localtime(j.created_at).strftime('%b %-d %H:%M'),
            'created_iso': j.created_at.isoformat(),
            'timing': j.data.get('timing'),
            'error': j.data.get('error', '')[:200] if j.data.get('error') else None,
            'page_group_id': j.data.get('result_page_group_id'),
        })
    has_active = any(j['status'] in ('queued', 'running') for j in result)
    return JsonResponse({
        'jobs': result,
        'claude_processes': list(claude_procs.values()) if has_active else [],
    })


@require_POST
@login_required
def job_abort(request, pk):
    """Abort a running/queued job. Worker detects status change and kills subprocess."""
    job = get_object_or_404(Job, id=pk)
    if job.status not in ('queued', 'running'):
        return JsonResponse({'error': 'Job is not active.'}, status=400)

    job.status = 'cancelled'
    job.data = {**job.data, 'error': 'Aborted by user'}
    job.save(update_fields=['status', 'data', 'modified_at'])

    if job.prompt:
        job.prompt.status = 'saved'
        job.prompt.save(update_fields=['status', 'modified_at'])

    return JsonResponse({'ok': True})


@require_POST
@login_required
def job_rerun(request, pk):
    """Rerun a completed/failed job — create new job with same prompt and definition."""
    job = get_object_or_404(Job, id=pk)
    if not job.prompt:
        return JsonResponse({'error': 'Job has no prompt.'}, status=400)

    from .generate import start_generation
    new_job = start_generation(job.prompt, job.definition)
    return JsonResponse({'ok': True, 'job_id': str(new_job.id)})


@require_POST
@login_required
def job_delete(request, pk):
    """Delete a completed/failed/cancelled job."""
    job = get_object_or_404(Job, id=pk)
    if job.status in ('queued', 'running'):
        return JsonResponse({'error': 'Abort the job first.'}, status=400)
    job.delete()
    return JsonResponse({'ok': True})


# ── Logs ─────────────────────────────────────────────────────────────────────

@login_required
def logs_view(request):
    logs = AppLog.objects.order_by('-timestamp')[:200]
    return render(request, 'codoc_app/logs.html', {'logs': logs})


# ── Definitions (was Config) ────────────────────────────────────────────────

def definitions_view(request):
    """Two-panel definitions management."""
    defs = JobDefinition.objects.filter(status__in=['active', 'paused'])
    return render(request, 'codoc_app/definitions.html', {'definitions': defs})


def definition_fragment(request, pk):
    """AJAX fragment: definition detail for right panel."""
    d = get_object_or_404(JobDefinition, id=pk)
    sp = None
    sp_gid = d.data.get('system_prompt_group_id')
    if sp_gid:
        sp = SystemPrompt.objects.filter(group_id=sp_gid, is_current=True).first()
    sysprompts = SystemPrompt.objects.filter(is_current=True)
    html = render_to_string('codoc_app/_definition_fragment.html', {
        'definition': d, 'sysprompt': sp, 'sysprompts': sysprompts,
    }, request=request)
    return HttpResponse(html)


@login_required
def definition_edit(request, pk=None):
    """Create or edit a definition. Returns JSON for AJAX."""
    if pk:
        d = get_object_or_404(JobDefinition, id=pk)
    else:
        d = None

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        model = request.POST.get('model', 'sonnet')
        effort = request.POST.get('effort', 'high')
        sp_group_id = request.POST.get('system_prompt_group_id', '')
        mcp_tools = request.POST.getlist('mcp_tools')
        timeout_min = request.POST.get('timeout_min', '30')

        if not name:
            return JsonResponse({'error': 'Name is required.'}, status=400)

        try:
            timeout_s = int(float(timeout_min)) * 60
        except (ValueError, TypeError):
            timeout_s = 1800

        data = {
            'model': model,
            'effort': effort,
            'mcp_tools': mcp_tools or ['lxr', 'github'],
            'timeout_s': timeout_s,
        }
        if sp_group_id:
            data['system_prompt_group_id'] = sp_group_id

        if d:
            d.name = name
            d.description = description
            d.data = {**d.data, **data}
            d.save()
        else:
            d = JobDefinition.objects.create(
                name=name, description=description, data=data,
            )

        return JsonResponse({'ok': True, 'id': str(d.id), 'name': d.name})

    # GET: return edit form fragment
    sysprompts = SystemPrompt.objects.filter(is_current=True)
    current_sp_content = ''
    if d:
        sp_gid = d.data.get('system_prompt_group_id')
        if sp_gid:
            sp = SystemPrompt.objects.filter(group_id=sp_gid, is_current=True).first()
            if sp:
                current_sp_content = sp.content
    html = render_to_string('codoc_app/_definition_edit_fragment.html', {
        'definition': d, 'sysprompts': sysprompts,
        'current_sp_content': current_sp_content,
    }, request=request)
    return HttpResponse(html)


@require_POST
@login_required
def definition_delete(request, pk):
    """Delete a definition (archives it if jobs reference it)."""
    d = get_object_or_404(JobDefinition, id=pk)
    if d.jobs.exists():
        d.status = 'archived'
        d.save(update_fields=['status', 'modified_at'])
    else:
        d.delete()
    return JsonResponse({'ok': True})


@require_POST
@login_required
def definition_copy(request, pk):
    """Copy a definition with name 'original - copy'."""
    d = get_object_or_404(JobDefinition, id=pk)
    new = JobDefinition.objects.create(
        name=f'{d.name} - copy',
        description=d.description,
        data=dict(d.data),
    )
    return JsonResponse({'ok': True, 'id': str(new.id), 'name': new.name})


# ── System Prompts (two-panel) ─────────────────────────────────────────────

def sysprompts_view(request):
    """Two-panel system prompt management."""
    sysprompts = SystemPrompt.objects.filter(is_current=True).order_by('name')
    return render(request, 'codoc_app/sysprompts.html', {'sysprompts': sysprompts})


def sysprompt_frag(request, group_id):
    """AJAX fragment: system prompt detail."""
    sp = get_object_or_404(SystemPrompt, group_id=group_id, is_current=True)
    versions = SystemPrompt.objects.filter(group_id=group_id).order_by('-version')
    html = render_to_string('codoc_app/_sysprompt_fragment.html', {
        'sp': sp, 'versions': versions,
    }, request=request)
    return HttpResponse(html)


def sysprompt_edit_frag(request, group_id=None):
    """AJAX fragment: system prompt edit form."""
    sp = None
    if group_id:
        sp = SystemPrompt.objects.filter(group_id=group_id, is_current=True).first()
    html = render_to_string('codoc_app/_sysprompt_edit_fragment.html', {
        'sp': sp,
    }, request=request)
    return HttpResponse(html)


@require_POST
@login_required
def sysprompt_save_api(request):
    """Save or create a system prompt. Creates new version if editing."""
    group_id = request.POST.get('group_id', '').strip()
    name = request.POST.get('name', '').strip()
    content = request.POST.get('content', '').strip()
    description = request.POST.get('description', '').strip()

    if not name or not content:
        return JsonResponse({'error': 'Name and content required.'}, status=400)

    if group_id:
        # Edit existing — create new version
        current = SystemPrompt.objects.filter(group_id=group_id, is_current=True).first()
        if current and content == current.content and name == current.name:
            return JsonResponse({'ok': True, 'group_id': group_id})
        if current:
            current.is_current = False
            current.save(update_fields=['is_current', 'modified_at'])
            new_version = current.version + 1
            base_data = current.data
        else:
            new_version = 1
            base_data = {}
        SystemPrompt.objects.create(
            group_id=group_id, version=new_version, is_current=True,
            name=name, content=content,
            data={**base_data, 'description': description, 'changed_by': request.user.username},
        )
    else:
        # New system prompt
        group_id = str(uuid.uuid4())
        SystemPrompt.objects.create(
            group_id=group_id, version=1, is_current=True,
            name=name, content=content,
            data={'description': description, 'created_by': request.user.username},
        )

    return JsonResponse({'ok': True, 'group_id': group_id})


@require_POST
@login_required
def sysprompt_delete(request, group_id):
    """Delete a system prompt (all versions)."""
    # Check if any definition references it
    from corun_app.models import JobDefinition
    refs = JobDefinition.objects.filter(data__system_prompt_group_id=str(group_id)).count()
    if refs:
        return JsonResponse({'error': f'Referenced by {refs} definition(s). Remove references first.'}, status=400)
    SystemPrompt.objects.filter(group_id=group_id).delete()
    return JsonResponse({'ok': True})


# ── About ────────────────────────────────────────────────────────────────────

def about_view(request):
    content = SiteContent.objects.filter(slug='about').first()
    return render(request, 'codoc_app/about.html', {'content': content})


@login_required
def about_edit(request):
    content, _ = SiteContent.objects.get_or_create(
        slug='about', defaults={'title': 'About', 'content': ''})

    if request.method == 'POST':
        text = request.POST.get('content', '').strip()
        content.content = text
        content.content_rendered = md_lib.markdown(text, extensions=['fenced_code', 'tables'])
        content.modified_by = request.user
        content.save()
        messages.success(request, 'About page updated.')
        return redirect('codoc:about')

    return render(request, 'codoc_app/about_edit.html', {'content': content})


# ── Account ──────────────────────────────────────────────────────────────────

@login_required
def account_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        theme = request.POST.get('theme', 'dark')
        if theme in ('dark', 'light'):
            profile.theme = theme
            profile.save(update_fields=['theme'])
            messages.success(request, f'Theme set to {theme}.')
        return redirect('codoc:account')

    return render(request, 'codoc_app/account.html', {'profile': profile})
