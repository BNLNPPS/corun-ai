"""Code documentation views — the first corun-ai application."""

import json
import logging
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

import markdown as md_lib

from corun_app.models import (
    AppLog, Comment, Job, JobDefinition, Page, Prompt, Section,
    SiteContent, SystemPrompt, UserProfile,
)

logger = logging.getLogger(__name__)


# ── Browse (two-panel home) ──────────────────────────────────────────────────

def home(request):
    """Two-panel browse: prompt versions with their pages nested underneath."""
    sections = Section.objects.filter(status='active').order_by('data__sort_order', 'name')

    for sec in sections:
        # Get current prompt versions (the group representatives)
        current_prompts = list(sec.prompts.filter(
            is_current=True,
        ).exclude(status='rejected').order_by('-created_at'))

        # Comment counts per prompt group (prompt-level only)
        from django.db.models import Count
        comment_counts = dict(
            Comment.objects.filter(
                prompt_group__in=[p.group_id for p in current_prompts],
                page__isnull=True,
            ).values_list('prompt_group').annotate(n=Count('id')).values_list('prompt_group', 'n')
        )

        # For each prompt group, get all versions + their pages
        browse_items = []
        # Also count page-level comments for each prompt group
        page_comment_by_group = dict(
            Comment.objects.filter(
                page__prompt__group_id__in=[p.group_id for p in current_prompts],
                page__isnull=False,
            ).values_list('page__prompt__group_id').annotate(n=Count('id')).values_list('page__prompt__group_id', 'n')
        ) if current_prompts else {}

        for cp in current_prompts:
            cp.comment_count = comment_counts.get(cp.group_id, 0) + page_comment_by_group.get(cp.group_id, 0)
            all_pages = list(Page.objects.filter(
                prompt__group_id=cp.group_id,
                is_current=True, status='published',
            ).select_related('prompt').order_by('-created_at'))
            # Group pages by prompt content (not version row)
            # Pages from same-text versions go together under current
            pages_by_content = {}
            for page in all_pages:
                pages_by_content.setdefault(page.prompt.content, []).append(page)
            # Current version gets pages matching its text
            cp.child_pages = pages_by_content.pop(cp.content, [])
            browse_items.append(cp)
            # Older versions only if they have pages with DIFFERENT text
            if pages_by_content:
                older = list(Prompt.objects.filter(
                    group_id=cp.group_id,
                ).exclude(id=cp.id).order_by('-version'))
                seen_content = set()
                for pv in older:
                    if pv.content in pages_by_content and pv.content not in seen_content:
                        pv.child_pages = pages_by_content[pv.content]
                        browse_items.append(pv)
                        seen_content.add(pv.content)

        # Page comment counts
        all_page_ids = []
        for item in browse_items:
            all_page_ids.extend(p.id for p in getattr(item, 'child_pages', []))
        if all_page_ids:
            page_comment_counts = dict(
                Comment.objects.filter(
                    page_id__in=all_page_ids,
                ).values_list('page_id').annotate(n=Count('id')).values_list('page_id', 'n')
            )
            for item in browse_items:
                for pg in getattr(item, 'child_pages', []):
                    pg.comment_count = page_comment_counts.get(pg.id, 0)

        # Orphaned pages (no prompt) — show as standalone items
        orphan_pages = list(Page.objects.filter(
            section=sec, prompt__isnull=True,
            is_current=True, status='published',
        ).order_by('-created_at'))

        sec.browse_items = browse_items
        sec.orphan_pages = orphan_pages

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


def prompt_info_fragment(request, group_id):
    """AJAX fragment: prompt info only (no pages) for Documents browse."""
    vid = request.GET.get('vid')
    if vid:
        prompt = get_object_or_404(Prompt, id=vid)
        # Show pages from same-text versions (same logic as browse tree)
        same_text_ids = list(Prompt.objects.filter(
            group_id=prompt.group_id, content=prompt.content
        ).values_list('id', flat=True))
        pages = list(Page.objects.filter(
            prompt_id__in=same_text_ids, is_current=True, status='published'
        ).order_by('-created_at'))
    else:
        prompt = get_object_or_404(Prompt, group_id=group_id, is_current=True)
        same_text_ids = list(Prompt.objects.filter(
            group_id=group_id, content=prompt.content
        ).values_list('id', flat=True))
        pages = list(Page.objects.filter(
            prompt_id__in=same_text_ids, is_current=True, status='published'
        ).order_by('-created_at'))
    # Active jobs for this prompt group
    active_jobs = list(Job.objects.filter(
        prompt__group_id=group_id,
        status__in=['queued', 'running'],
    ).select_related('definition').order_by('-created_at'))

    comments = Comment.objects.filter(prompt_group=group_id, page__isnull=True).select_related('author')
    # Comments on pages belonging to this prompt, grouped by page
    page_ids = [p.id for p in pages]
    page_comments_raw = list(Comment.objects.filter(
        page_id__in=page_ids
    ).select_related('author', 'page').order_by('-created_at')) if page_ids else []
    # Group by page, preserving page order
    from collections import OrderedDict
    pc_by_page = OrderedDict()
    for c in page_comments_raw:
        pc_by_page.setdefault(c.page_id, []).append(c)
    page_comments_grouped = []
    for pg in pages:
        if pg.id in pc_by_page:
            page_comments_grouped.append((pg, pc_by_page[pg.id]))
    html = render_to_string('codoc_app/_prompt_info_fragment.html', {
        'prompt': prompt, 'pages': pages, 'active_jobs': active_jobs,
        'comments': comments, 'page_comments': page_comments_raw,
        'page_comments_grouped': page_comments_grouped,
    }, request=request)
    return HttpResponse(html)


def prompt_fragment(request, group_id):
    """AJAX fragment: prompt detail for right panel.

    If ?vid=<uuid> is passed, show that specific prompt version and only
    its pages. Otherwise show current version with all pages.
    """
    vid = request.GET.get('vid')
    if vid:
        prompt = get_object_or_404(Prompt, id=vid)
        pages = Page.objects.filter(prompt=prompt, is_current=True)
    else:
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
    comments = Comment.objects.filter(page=page).select_related('author')
    html = render_to_string('codoc_app/_page_fragment.html', {
        'page': page, 'job_def': job_def, 'comments': comments,
    }, request=request)
    return HttpResponse(html)


# ── Inline editor fragment ──────────────────────────────────────────────────

def editor_fragment(request, group_id=None):
    """AJAX fragment: inline editor in right panel. group_id=None for new prompt."""
    sections = Section.objects.filter(status='active').order_by('data__sort_order', 'name')
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
        job = start_generation(prompt, job_def, triggered_by=request.user)
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
    job = start_generation(prompt, job_def, triggered_by=request.user)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'job_id': str(job.id)})
    return redirect('codoc:queue')


# ── Prepare prompt (standalone page, fallback) ──────────────────────────────

@login_required
def prepare_prompt(request):
    """Standalone prepare prompt page.

    GET ?source_job=<uuid> — pre-fill from an existing job's prompt and
    definition so a user can Resubmit via this panel and change the
    sysprompt / model before actually generating.
    """
    # Show all sections/definitions (not just active) so resubmit from an
    # archived/inactive one still surfaces the real choice — marked visibly
    # as inactive. Order: active first, then everything else.
    from django.db.models import Case, When, IntegerField, Value
    sections = list(Section.objects.annotate(
        _rank=Case(When(status='active', then=Value(0)),
                   default=Value(1), output_field=IntegerField()),
    ).order_by('_rank', 'data__sort_order', 'name'))
    for sec in sections:
        sec.display_label = sec.title or sec.name
        if sec.status != 'active':
            sec.display_label = f"{sec.display_label} ({sec.status})"

    definitions = list(JobDefinition.objects.annotate(
        _rank=Case(When(status='active', then=Value(0)),
                   default=Value(1), output_field=IntegerField()),
    ).order_by('_rank', 'name'))
    from .generate import get_or_create_default_def
    job_def = get_or_create_default_def()

    # Attach a summary of each definition's sysprompt for the live-updating
    # config summary in the template.
    sp_groups = {d.data.get('system_prompt_group_id') for d in definitions if d.data.get('system_prompt_group_id')}
    sysprompt_by_group = {
        sp.group_id: sp for sp in
        SystemPrompt.objects.filter(group_id__in=sp_groups, is_current=True)
    }
    for d in definitions:
        sp = sysprompt_by_group.get(d.data.get('system_prompt_group_id'))
        d.display_model = d.data.get('model', '')
        d.display_tools = ', '.join(d.data.get('mcp_tools', []) or [])
        d.display_sp_name = sp.name if sp else ''
        d.display_sp_version = sp.version if sp else ''
        d.display_sp_group = str(sp.group_id) if sp else ''
        d.display_label = d.name if d.status == 'active' else f"{d.name} ({d.status})"

    # Default sysprompt for the top-of-page config summary (no def selected yet).
    sysprompt = None
    sp_gid = job_def.data.get('system_prompt_group_id')
    if sp_gid:
        sysprompt = SystemPrompt.objects.filter(group_id=sp_gid, is_current=True).first()

    # Pre-fill context (GET only; POST uses request.POST values directly).
    # Two supported GET modes:
    #   ?source_job=<uuid>  — pull section/content/definition from an existing job.
    #   ?section_id=<uuid>&definition_id=<uuid>&content=<str>  — direct prefill
    #     (e.g. from the Submit PR review button on /doc/prs/).
    prefill = {'section_id': '', 'content': '', 'definition_id': ''}
    if request.method == 'GET':
        source_job_id = request.GET.get('source_job', '').strip()
        if source_job_id:
            src = Job.objects.filter(id=source_job_id).select_related(
                'prompt', 'prompt__section', 'definition',
            ).first()
            if src and src.prompt:
                prefill = {
                    'section_id': str(src.prompt.section_id),
                    'content': src.prompt.content,
                    'definition_id': str(src.definition_id),
                }
        else:
            prefill = {
                'section_id': request.GET.get('section_id', '').strip(),
                'content': request.GET.get('content', ''),
                'definition_id': request.GET.get('definition_id', '').strip(),
            }

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
                'prefill': {
                    'section_id': section_id or '',
                    'content': content,
                    'definition_id': definition_id,
                },
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
            start_generation(prompt, jd, triggered_by=request.user)
            return redirect('codoc:queue')
        else:
            messages.success(request, 'Prompt saved.')
            return redirect('codoc:home')

    return render(request, 'codoc_app/prepare.html', {
        'sections': sections, 'definitions': definitions,
        'sysprompt': sysprompt, 'job_def': job_def,
        'prefill': prefill,
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
    sections = Section.objects.filter(status='active').order_by('data__sort_order', 'name')
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


# ── Version API ───────────────────────────────────────────────────────────────

def prompt_version_api(request, group_id, version):
    """Return a specific prompt version's content as JSON."""
    p = get_object_or_404(Prompt, group_id=group_id, version=version)
    return JsonResponse({
        'content': p.content, 'version': p.version,
        'created_at': p.created_at.strftime('%b %-d %H:%M'),
        'is_current': p.is_current,
    })


def sysprompt_version_api(request, group_id, version):
    """Return a specific system prompt version's content as JSON."""
    sp = get_object_or_404(SystemPrompt, group_id=group_id, version=version)
    return JsonResponse({
        'content': sp.content, 'name': sp.name, 'version': sp.version,
        'created_at': sp.created_at.strftime('%b %-d %H:%M'),
        'is_current': sp.is_current,
        'description': (sp.data or {}).get('description', ''),
    })


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
    comments = list(Comment.objects.filter(page=page).select_related('author'))
    return render(request, 'codoc_app/page.html', {'page': page, 'comments': comments})


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
    from django.db.models import Count
    from django.utils import timezone as tz

    jobs = list(Job.objects.select_related(
        'definition', 'prompt__submitted_by', 'triggered_by',
    ).order_by('-created_at')[:50])

    # Pre-compute "has any comments" per job in two batched queries — one
    # for prompt-group comments, one for result-page comments. A job is
    # marked commented if either applies.
    prompt_groups = {j.prompt.group_id for j in jobs if j.prompt}
    result_page_groups = {
        j.data.get('result_page_group_id') for j in jobs
        if j.data.get('result_page_group_id')
    }
    prompt_groups_with_comments = set(
        Comment.objects.filter(prompt_group__in=prompt_groups)
        .values_list('prompt_group', flat=True).distinct()
    )
    page_id_by_group = dict(
        Page.objects.filter(group_id__in=result_page_groups, is_current=True)
        .values_list('group_id', 'id')
    )
    page_ids_with_comments = set(
        Comment.objects.filter(page_id__in=page_id_by_group.values())
        .values_list('page_id', flat=True).distinct()
    )
    page_groups_with_comments = {
        str(g) for g, pid in page_id_by_group.items() if pid in page_ids_with_comments
    }

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
        # For running jobs, modified_at is when the worker flipped status
        # queued→running (worker only saves the Job at start and end, so
        # modified_at is unambiguous mid-run). For queued jobs no such
        # transition has happened — leave started_* fields null and let
        # the UI render '—' instead of lying about elapsed time.
        started = None
        started_iso = None
        if j.status == 'running':
            started = tz.localtime(j.modified_at).strftime('%b %-d %H:%M')
            started_iso = j.modified_at.isoformat()
        result_page_group = j.data.get('result_page_group_id')
        commented = (
            (j.prompt and j.prompt.group_id in prompt_groups_with_comments)
            or (result_page_group and str(result_page_group) in page_groups_with_comments)
        )
        result.append({
            'id': str(j.id),
            'status': j.status,
            'definition': j.definition.name,
            'prompt': j.prompt.content[:80] if j.prompt else j.data.get('prompt_content', '')[:80],
            'commented': bool(commented),
            'created': tz.localtime(j.created_at).strftime('%b %-d %H:%M'),
            'created_iso': j.created_at.isoformat(),
            'started': started,
            'started_iso': started_iso,
            'timing': j.data.get('timing'),
            'error': j.data.get('error', '')[:200] if j.data.get('error') else None,
            'page_group_id': j.data.get('result_page_group_id'),
            'user': (
                j.triggered_by.username if j.triggered_by
                else (j.prompt.submitted_by.username if j.prompt and j.prompt.submitted_by else '')
            ),
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
    """Rerun a completed/failed job — create new job with same prompt and definition.

    If the system prompt has drifted since the original run, the first
    POST returns {needs_choice, original_version, current_version} and
    the client must POST again with sp_version set to either value to
    explicitly pick latest or original (provenance/reproducibility).
    """
    job = get_object_or_404(Job, id=pk)
    if not job.prompt:
        return JsonResponse({'error': 'Job has no prompt.'}, status=400)

    sp_group_id = job.definition.data.get('system_prompt_group_id')
    original_version = job.data.get('system_prompt_version')
    requested = request.POST.get('sp_version', '').strip()

    # Drift detection — only on first POST (no sp_version supplied)
    if not requested and sp_group_id and original_version is not None:
        cur = SystemPrompt.objects.filter(
            group_id=sp_group_id, is_current=True,
        ).first()
        if cur and cur.version != original_version:
            return JsonResponse({
                'needs_choice': True,
                'original_version': original_version,
                'current_version': cur.version,
            })

    sp_version_override = None
    if requested:
        try:
            sp_version_override = int(requested)
        except ValueError:
            return JsonResponse({'error': 'invalid sp_version'}, status=400)

    from .generate import start_generation
    new_job = start_generation(
        job.prompt, job.definition,
        triggered_by=request.user,
        system_prompt_version=sp_version_override,
    )
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


# ── Comments ──────────────────────────────────────────────────────────────────

@require_POST
@login_required
def comment_post(request):
    """Post a comment on a prompt group, a page, or standalone."""
    content = request.POST.get('content', '').strip()
    prompt_group = request.POST.get('prompt_group', '').strip()
    page_id = request.POST.get('page_id', '').strip()
    if not content:
        return JsonResponse({'error': 'Content required.'}, status=400)
    kwargs = {'author': request.user, 'content': content}
    if page_id:
        kwargs['page'] = get_object_or_404(Page, id=page_id)
    if prompt_group and prompt_group != 'global':
        kwargs['prompt_group'] = prompt_group
    Comment.objects.create(**kwargs)
    return JsonResponse({'ok': True})


@require_POST
@login_required
def comment_delete(request, pk):
    """Delete a comment. Author only."""
    comment = get_object_or_404(Comment, id=pk)
    if comment.author != request.user:
        return JsonResponse({'error': 'Not your comment.'}, status=403)
    comment.delete()
    return JsonResponse({'ok': True})


def comments_page(request):
    """All comments across the app, reverse chronological."""
    comments = list(Comment.objects.select_related(
        'author', 'page', 'page__prompt',
    ).order_by('-created_at')[:200])
    # Resolve prompt labels — from prompt_group or from page.prompt
    prompt_groups = set()
    for c in comments:
        if c.prompt_group:
            prompt_groups.add(c.prompt_group)
        if c.page and c.page.prompt:
            prompt_groups.add(c.page.prompt.group_id)
    prompt_map = {}
    if prompt_groups:
        for p in Prompt.objects.filter(group_id__in=prompt_groups, is_current=True):
            prompt_map[p.group_id] = p.content[:80]
    for c in comments:
        if c.prompt_group:
            c.prompt_label = prompt_map.get(c.prompt_group, '')
        elif c.page and c.page.prompt:
            c.prompt_label = prompt_map.get(c.page.prompt.group_id, '')
            c.prompt_group_resolved = c.page.prompt.group_id
        else:
            c.prompt_label = ''
    return render(request, 'codoc_app/comments.html', {'comments': comments})


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
    from corun_app.models import (
        GEMINI_MODELS, REMOTE_MODELS, MCP_SERVERS, REMOTE_EXTRA_MCP_LABELS,
    )
    # Resolve MCP tool keys to labels. Look in MCP_SERVERS first (local-
    # execution registry), then fall back to REMOTE_EXTRA_MCP_LABELS for
    # tools that only exist on the Mac side.
    def _mcp_label(key):
        if key in MCP_SERVERS:
            return MCP_SERVERS[key]['label']
        if key in REMOTE_EXTRA_MCP_LABELS:
            return REMOTE_EXTRA_MCP_LABELS[key]
        return None
    mcp_tools = d.data.get('mcp_tools', [])
    labels = [lbl for lbl in (_mcp_label(k) for k in mcp_tools) if lbl]
    d.data['mcp_tools_display'] = ', '.join(labels) or 'None'
    model = d.data.get('model', 'sonnet')
    d.data['is_remote'] = model in REMOTE_MODELS
    if model in REMOTE_MODELS:
        d.data['cli_preview'] = (
            f'POST tjai /api/work/submit {{capability:{model}}} '
            f'→ remote Mac ollama ({model})'
        )
    elif model in GEMINI_MODELS:
        d.data['cli_preview'] = f'gemini -m {model} --yolo -p "<prompt>"'
    else:
        d.data['cli_preview'] = f'claude -p --model {model} --system-prompt "..." --output-format text'
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
        from corun_app.models import REMOTE_MODELS, REMOTE_FIXED_MCP_TOOLS
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        model = request.POST.get('model', 'sonnet')
        effort = request.POST.get('effort', 'high')
        sp_group_id = request.POST.get('system_prompt_group_id', '')
        mcp_tools = request.POST.getlist('mcp_tools')
        timeout_min = request.POST.get('timeout_min', '60')

        if not name:
            return JsonResponse({'error': 'Name is required.'}, status=400)

        try:
            timeout_s = int(float(timeout_min)) * 60
        except (ValueError, TypeError):
            timeout_s = 3600

        # Remote-dispatched models (Gemma, Qwen, …) have a fixed MCP set
        # wired into the Mac-side runner; anything the client sent is discarded.
        if model in REMOTE_MODELS:
            effective_mcp_tools = list(REMOTE_FIXED_MCP_TOOLS)
        else:
            effective_mcp_tools = mcp_tools or ['lxr', 'github']

        data = {
            'model': model,
            'effort': effort,
            'mcp_tools': effective_mcp_tools,
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
    from corun_app.models import (
        MODEL_CHOICES, MCP_SERVERS, REMOTE_MODELS, REMOTE_FIXED_MCP_TOOLS,
        REMOTE_EXTRA_MCP_LABELS,
    )
    from collections import OrderedDict
    _mg = OrderedDict()
    for value, label, group in MODEL_CHOICES:
        _mg.setdefault(group, []).append((value, label))
    model_groups = list(_mg.items())
    sp_contents = {str(sp.group_id): sp.content for sp in sysprompts}
    html = render_to_string('codoc_app/_definition_edit_fragment.html', {
        'definition': d, 'sysprompts': sysprompts,
        'current_sp_content': current_sp_content,
        'model_groups': model_groups,
        'mcp_choices': [(k, v['label']) for k, v in MCP_SERVERS.items()],
        # Mac-only MCPs — rendered as additional checkboxes after the
        # regular ones. The template tags them with data-remote-only so
        # the JS can hide them in non-remote mode (where they have no
        # local-execution config and would silently fail if selected).
        'remote_extra_choices': list(REMOTE_EXTRA_MCP_LABELS.items()),
        'remote_models_json': json.dumps(sorted(REMOTE_MODELS)),
        'remote_fixed_mcp_json': json.dumps(list(REMOTE_FIXED_MCP_TOOLS)),
        'sp_contents_json': json.dumps(sp_contents),
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
    sysprompts = SystemPrompt.objects.filter(is_current=True).order_by('-modified_at')
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


# ── Job artifacts ─────────────────────────────────────────────────────────────

def job_thinking(request, job_id):
    """Serve the thinking trace file for a job."""
    import os
    path = os.path.join('/var/www/corun-ai/data/jobs', str(job_id), 'thinking.txt')
    try:
        with open(path) as f:
            return HttpResponse(f.read(), content_type='text/plain')
    except FileNotFoundError:
        return HttpResponse('No thinking trace available.', content_type='text/plain', status=404)


# ── About ────────────────────────────────────────────────────────────────────

def about_view(request):
    content = SiteContent.objects.filter(slug='about', is_current=True).first()
    versions = list(SiteContent.objects.filter(slug='about').order_by('-version'))
    return render(request, 'codoc_app/about.html', {'content': content, 'versions': versions})


def about_version_api(request, version):
    """Return a specific about page version's content as JSON."""
    sc = get_object_or_404(SiteContent, slug='about', version=version)
    return JsonResponse({
        'content': sc.content, 'version': sc.version,
        'modified_at': sc.modified_at.strftime('%b %-d %H:%M'),
        'is_current': sc.is_current,
    })


@login_required
def about_edit(request):
    current = SiteContent.objects.filter(slug='about', is_current=True).first()
    if not current:
        current = SiteContent.objects.create(
            slug='about', title='About', content='', is_current=True, version=1)

    if request.method == 'POST':
        text = request.POST.get('content', '').strip()
        if text != current.content:
            # Mark old as superseded, create new version
            current.is_current = False
            current.save(update_fields=['is_current'])
            SiteContent.objects.create(
                slug='about', title='About',
                version=current.version + 1, is_current=True,
                content=text,
                content_rendered=md_lib.markdown(text, extensions=['fenced_code', 'tables', 'toc']),
                modified_by=request.user,
            )
        messages.success(request, 'About page updated.')
        return redirect('codoc:about')

    return render(request, 'codoc_app/about_edit.html', {'content': current})


# ── ePIC PRs ─────────────────────────────────────────────────────────────────

# All repos indexed by the ePIC LXR code browser
_EPIC_REPOS = [
    "eic/acts", "eic/algorithms", "eic/containers", "eic/DD4hep",
    "eic/DEMPgen", "eic/detector_benchmarks", "eic/drich-dev",
    "eic/EDM4eic", "eic/edpm", "eic/eic-shell", "eic/eic-spack",
    "eic/eic.github.io", "eic/EICrecon", "eic/epic", "eic/epic-capybara",
    "eic/epic-data", "eic/epic-lfhcal-tbana", "eic/epic-prod",
    "eic/estarlight", "eic/firebird", "eic/firehose", "eic/geant4",
    "eic/HEPMC_Merger", "eic/image_browser", "eic/irt",
    "JeffersonLab/JANA2", "eic/job_submission_condor",
    "eic/job_submission_slurm", "eic/JPsiDataSet", "eic/LQGENEP",
    "eic/npsim", "eic/pfRICH", "eic/phoenix-eic-event-display",
    "eic/physics_benchmarks", "eic/run-cvmfs-osg-eic-shell",
    "eic/simulation_campaign_datasets", "eic/simulation_campaign_hepmc3",
    "eic/simulation_campaign_single", "eic/snippets",
    "eic/trigger-gitlab-ci", "eic/tutorial-analysis",
    "eic/tutorial-developing-benchmarks",
    "eic/tutorial-geometry-development-using-dd4hep",
    "eic/tutorial-jana2", "eic/tutorial-reconstruction-algorithms",
    "eic/tutorial-setting-up-environment",
    "eic/tutorial-simulations-using-ddsim-and-geant4", "eic/UpsilonGen",
]


def epic_prs_api(request):
    """Serve cached PR data. Cache is kept warm by a tjai-scheduled
    refresher (15 min delta, nightly full rebuild). If the cache is
    missing, fire a one-shot full rebuild in the background and tell
    the client we're refreshing; if the cache is stale past the warm
    window, fire a delta refresh in the background but still return
    what we have.
    """
    import os
    from .prs_cache import load_cache, CACHE_PATH, SCHEMA_VERSION

    data = load_cache()

    if data is None:
        # Cold start — no cache on disk or schema mismatch. Fire a full
        # rebuild in the background and return a stub the JS can poll on.
        subprocess.Popen(
            [sys.executable, '-c',
             'import django, os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "corun_project.settings"); '
             'django.setup(); from codoc_app.prs_cache import refresh_full; refresh_full()'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return JsonResponse({
            'open': {}, 'closed': {}, 'generated': None,
            'status': 'refreshing', 'schema_version': SCHEMA_VERSION,
        })

    # Stale if the scheduled refresher hasn't run recently — usually
    # means the scheduler is broken or a human is looking seconds after
    # a deploy. Fire a delta in the background; return existing data now.
    try:
        gen = datetime.fromisoformat(data['generated'])
        stale = (datetime.now(timezone.utc) - gen).total_seconds() > 1800  # 30 min
    except (KeyError, ValueError):
        stale = True

    if stale:
        subprocess.Popen(
            [sys.executable, '-c',
             'import django, os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "corun_project.settings"); '
             'django.setup(); from codoc_app.prs_cache import refresh_delta; refresh_delta()'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    return JsonResponse(data)


@require_POST
@login_required
def epic_prs_refresh_api(request):
    """Forced delta refresh triggered by the Update button.

    Precheck the GitHub core rate limit; only run if we have comfortable
    headroom. A full rebuild is ~176 calls; RATE_LIMIT_FLOOR defines the
    minimum remaining budget we insist on before proceeding.
    """
    from .prs_cache import refresh_delta, check_rate_limit, RATE_LIMIT_FLOOR

    rl = check_rate_limit()
    remaining = rl.get('remaining')
    if remaining is None:
        return JsonResponse({
            'ok': False,
            'reason': f'rate-limit precheck failed: {rl.get("error") or "no data"}',
            'rate_limit': rl,
        }, status=503)
    if remaining < RATE_LIMIT_FLOOR:
        return JsonResponse({
            'ok': False,
            'reason': (
                f'insufficient GitHub rate-limit headroom '
                f'({remaining} < floor {RATE_LIMIT_FLOOR}); resets {rl.get("reset")}'
            ),
            'rate_limit': rl,
        }, status=429)

    try:
        data = refresh_delta()
    except Exception as e:
        return JsonResponse({'ok': False, 'reason': f'refresh failed: {e}'}, status=500)

    data['ok'] = True
    data['rate_limit'] = rl
    return JsonResponse(data)


def epic_prs_view(request):
    # Bind the PR-review Section + JobDefinition UUIDs into the page context
    # so the Submit PR review button can POST directly without an extra fetch.
    sec = Section.objects.filter(name='pr-review', status='active').first()
    jdef = JobDefinition.objects.filter(name='codoc-pr-review', status='active').first()
    return render(request, 'codoc_app/epic_prs.html', {
        'repo_count': len(_EPIC_REPOS),
        'pr_review_section_id': sec.id if sec else '',
        'pr_review_definition_id': jdef.id if jdef else '',
    })


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
