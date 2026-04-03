"""
corun-ai data models.

All models: UUID pk, data JSONField, created_at/modified_at.
Versioning is in-table: each row is a version, group_id ties versions together.
"""

import uuid

from django.conf import settings

# Model registry: (value, label, group). Single source of truth.
MODEL_CHOICES = [
    ('opus', 'Opus', 'Claude'),
    ('sonnet', 'Sonnet', 'Claude'),
    ('haiku', 'Haiku', 'Claude'),
    ('gemini-2.5-flash', 'Gemini 2.5 Flash', 'Gemini'),
    ('gemini-2.5-pro', 'Gemini 2.5 Pro', 'Gemini'),
]

GEMINI_MODELS = {m[0] for m in MODEL_CHOICES if m[2] == 'Gemini'}

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
