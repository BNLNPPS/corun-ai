"""
corun-ai data models.

All models: UUID pk, data JSONField, created_at/modified_at.
Versioning is in-table: each row is a version, group_id ties versions together.
"""

import uuid

from django.conf import settings

# Model registry: (value, label, group). Single source of truth.
# 'Gemma' and 'Qwen' models (and any other open-source family hosted on
# Torre's Mac Studio) do not run locally here — they are dispatched to the
# remote worker via tjai's /api/work/submit endpoint. The set of such
# families is REMOTE_MODELS below.
MODEL_CHOICES = [
    ('opus', 'Opus', 'Claude'),
    ('sonnet', 'Sonnet', 'Claude'),
    ('haiku', 'Haiku', 'Claude'),
    ('gemini-2.5-flash', 'Gemini 2.5 Flash', 'Gemini'),
    ('gemini-2.5-pro', 'Gemini 2.5 Pro', 'Gemini'),
    ('deepseek-v4-flash', 'DeepSeek V4 Flash', 'DeepSeek'),
    ('deepseek-v4-pro', 'DeepSeek V4 Pro', 'DeepSeek'),
    ('gemma4', 'gemma4', 'Gemma'),
    ('gemma4-fast', 'gemma4-fast', 'Gemma'),
    ('qwen', 'qwen', 'Qwen'),
]

GEMINI_MODELS = {m[0] for m in MODEL_CHOICES if m[2] == 'Gemini'}
DEEPSEEK_MODELS = {m[0] for m in MODEL_CHOICES if m[2] == 'DeepSeek'}
GEMMA_MODELS = {m[0] for m in MODEL_CHOICES if m[2] == 'Gemma'}
QWEN_MODELS = {m[0] for m in MODEL_CHOICES if m[2] == 'Qwen'}

# Models dispatched to the remote worker (Mac Studio via tjai
# /api/work/submit) rather than run as a local subprocess. Currently all
# ollama-hosted open-source families. The dispatch path, fixed MCP set,
# and UI constraints are shared across the whole set.
REMOTE_MODELS = GEMMA_MODELS | QWEN_MODELS

# Mac-only MCP tools — labels for tools the Mac's tj_agent dispatcher
# advertises to remote-model runs but that corun-ai never spawns locally
# (so they have no entry in MCP_SERVERS, which is the local-execution
# registry). Their config lives on the Mac side; this dict exists only
# so the codoc UI can show the user what remote runs will actually have
# available.
REMOTE_EXTRA_MCP_LABELS = {
    'fetch':      'Fetch (HTTP)',
    'npp_search': 'NPP Search (Google CSE)',
    'web_search': 'Web Search (SerpAPI)',
}

# Remote-model jobs run on Torre's Mac Studio via the tjai remote-worker
# pipeline. Every tool the Mac advertises is always available — lxr/github
# are also in MCP_SERVERS (they happen to have local-execution configs
# too), the rest live in REMOTE_EXTRA_MCP_LABELS only. The codoc
# definition UI locks remote-model definitions to exactly this set,
# ticked and not editable. Keep this list in sync with what the Mac's
# tj_agent.dispatcher actually wires up — that's the source of truth.
REMOTE_FIXED_MCP_TOOLS = ['lxr', 'github', 'fetch', 'npp_search', 'web_search']

# Available MCP servers: (key, label, config_dict)
MCP_SERVERS = {
    'lxr': {
        'label': 'LXR Code Browser',
        'config': {
            'command': '/home/admin/github/lxr-mcp-server/.venv/bin/python',
            'args': ['/home/admin/github/lxr-mcp-server/lxr_mcp_server.py'],
        },
    },
    'github': {
        'label': 'GitHub',
        'config': {
            'command': '/home/admin/bin/github-mcp-server',
            'args': ['stdio'],
        },
    },
    'swf-testbed': {
        'label': 'SWF Testbed (PanDA, PCS, Workflows)',
        'config': {
            'type': 'http',
            'url': 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/',
        },
    },
    'xrootd': {
        'label': 'XRootD (EIC production files)',
        'config': {
            'command': 'node',
            'args': ['/home/admin/github/xrootd-mcp-server/build/src/index.js'],
            'env': {
                'XROOTD_SERVER': 'root://dtn-eic.jlab.org',
                'XROOTD_BASE_DIR': '/volatile/eic/EPIC',
            },
        },
    },
    'rucio-jlab': {
        'label': 'Rucio JLab (data management)',
        'config': {
            'command': '/home/admin/github/rucio-eic-mcp-server/.venv/bin/rucio-eic-mcp',
            'args': [],
            'env': {
                'RUCIO_URL': 'https://rucio-server.jlab.org:443',
                'RUCIO_AUTH_TYPE': 'userpass',
                'RUCIO_ACCOUNT': 'eicread',
                'RUCIO_USERNAME': 'eicread',
                'RUCIO_PASSWORD': 'eicread',
            },
        },
    },
}
from django.db import models


# ── Content Models ────────────────────────────────────────────────────────────


class Section(models.Model):
    """Topical area / collection for organizing content."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)        # slug: "pid", "tracking"
    title = models.CharField(max_length=200)                    # display: "PID Algorithms"
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=50, default='active')  # active, archived
    data = models.JSONField(default=dict, blank=True)           # ordering, classification
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.title or self.name


class Prompt(models.Model):
    """User-submitted prompt. Each row is a version."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group_id = models.UUIDField(db_index=True)                  # shared across versions
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)

    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name='prompts')
    content = models.TextField()
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='prompts')
    status = models.CharField(max_length=50, default='pending')
    # status values: pending, approved, generating, published, rejected
    data = models.JSONField(default=dict, blank=True)
    # data keys: tags, official, release_tag, subsystem, votes
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('group_id', 'version')
        indexes = [models.Index(fields=['group_id', '-version'])]
        ordering = ['-created_at']

    def __str__(self):
        return f"Prompt {self.group_id} v{self.version}"


class Page(models.Model):
    """AI-generated content page. Each row is a version."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group_id = models.UUIDField(db_index=True)                  # shared across versions
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)

    prompt = models.ForeignKey(
        Prompt, on_delete=models.SET_NULL, null=True, blank=True, related_name='pages')
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name='pages')
    content = models.TextField()                                    # generated content
    content_rendered = models.TextField(blank=True, default='')     # rendered output (cached)
    status = models.CharField(max_length=50, default='published')
    # status values: published, superseded, archived
    data = models.JSONField(default=dict, blank=True)
    # data keys: format, release_tag, generation_model, official, generation_time_s,
    #            job_id, sources
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('group_id', 'version')
        indexes = [models.Index(fields=['group_id', '-version'])]
        ordering = ['-created_at']

    def __str__(self):
        return f"Page {self.group_id} v{self.version}"


class Comment(models.Model):
    """Community discussion on a prompt or page."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(Page, on_delete=models.CASCADE, null=True, blank=True, related_name='comments')
    prompt_group = models.UUIDField(null=True, blank=True, db_index=True)  # prompt group_id
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='comments')
    content = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Comment by {self.author} on {self.page}"


# ── System Prompt ─────────────────────────────────────────────────────────────


class SystemPrompt(models.Model):
    """Reusable AI system prompt. Each row is a version."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group_id = models.UUIDField(db_index=True)
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)

    name = models.CharField(max_length=200)
    content = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    # data keys: description, model_hints, changed_by
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('group_id', 'version')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} v{self.version}"


# ── Job System (Gen3 Scheduler) ──────────────────────────────────────────────


class JobDefinition(models.Model):
    """Reusable template: what to do, when, with what model/tools."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=50, default='active')  # active, paused, archived
    data = models.JSONField(default=dict, blank=True)
    # data keys: trigger, scheduled_time, interval_hours,
    #            model, model_config, tools, system_prompt_id,
    #            steps (templates), cost_tracking
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-modified_at']

    def __str__(self):
        return self.name


class Job(models.Model):
    """Single execution instance. Every run is a row — full history."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        JobDefinition, on_delete=models.PROTECT, related_name='jobs')
    prompt = models.ForeignKey(
        Prompt, on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='triggered_jobs')
    # Who initiated this run. For original generation this is the same as
    # prompt.submitted_by; for reruns it's whoever clicked Rerun. Null on
    # legacy rows — fall back to prompt.submitted_by for display.
    status = models.CharField(max_length=50, default='queued')
    # status values: queued, running, completed, failed, cancelled
    data = models.JSONField(default=dict, blank=True)
    # data keys: error, result_page_id, timing, tokens, system_prompt_version
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Job {self.id} ({self.status})"


class JobStep(models.Model):
    """Individual step within a job. Deterministic execution order."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='steps')
    phase = models.PositiveIntegerField(default=1)
    step_num = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    step_type = models.CharField(max_length=50, default='script')
    # step_type values: ai, script, human, external, agent
    status = models.CharField(max_length=50, default='pending')
    # status values: pending, running, waiting, completed, failed, skipped
    data = models.JSONField(default=dict, blank=True)
    # data keys: config, output, error, timing, tokens, artifacts
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('job', 'step_num')
        ordering = ['job', 'phase', 'step_num']

    def __str__(self):
        return f"Step {self.step_num}: {self.name} ({self.status})"


# ── Logging ───────────────────────────────────────────────────────────────────


class AppLog(models.Model):
    """Structured application log. Zero silent failures."""
    source = models.CharField(max_length=100, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    level = models.IntegerField()
    levelname = models.CharField(max_length=20)
    message = models.TextField()
    extra_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['source', '-timestamp'])]

    def __str__(self):
        return f"[{self.levelname}] {self.source}: {self.message[:80]}"


# ── User Profile ─────────────────────────────────────────────────────────────


class UserProfile(models.Model):
    """User preferences. One per user."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    theme = models.CharField(max_length=10, default='dark')  # dark, light
    data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.user.username} profile"


# ── Site Content ─────────────────────────────────────────────────────────────


class SiteContent(models.Model):
    """Editable site content blocks (about page, etc.). In-table versioning."""
    slug = models.SlugField()
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    title = models.CharField(max_length=200)
    content = models.TextField()
    content_rendered = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-version']
        indexes = [models.Index(fields=['slug', '-version'])]

    def __str__(self):
        return f"{self.title} v{self.version}"
