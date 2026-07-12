"""
DRF serializers for the corun-ai REST API.
"""

import uuid as uuid_lib
from urllib.parse import urlparse

from rest_framework import serializers

from corun_app.models import (
    MCP_SERVERS, MODEL_CHOICES, REMOTE_EXTRA_MCP_LABELS,
    Comment, Job, JobDefinition, JobNotificationSubscription, Page, PageTag,
    Prompt, Section, SystemPrompt,
)


class SectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Section
        fields = ['id', 'name', 'title', 'description']


class PromptSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Prompt
        fields = ['id', 'group_id', 'version', 'is_current', 'content', 'status', 'created_at']


class SectionDetailSerializer(serializers.ModelSerializer):
    prompts = serializers.SerializerMethodField()

    class Meta:
        model = Section
        fields = ['id', 'name', 'title', 'description', 'prompts']

    def get_prompts(self, obj):
        qs = obj.prompts.filter(is_current=True).exclude(status='rejected').order_by('-created_at')
        return PromptSummarySerializer(qs, many=True).data


class PromptDetailSerializer(serializers.ModelSerializer):
    section_name = serializers.CharField(source='section.name', read_only=True)

    class Meta:
        model = Prompt
        fields = [
            'id', 'group_id', 'version', 'is_current',
            'section', 'section_name', 'content', 'status',
            'data', 'created_at', 'modified_at',
        ]


class PageDetailSerializer(serializers.ModelSerializer):
    section_name = serializers.CharField(source='section.name', read_only=True)
    title = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = [
            'id', 'group_id', 'version', 'is_current',
            'section', 'section_name', 'title', 'content', 'content_rendered',
            'status', 'tags', 'data', 'created_at', 'modified_at',
        ]

    def get_title(self, obj):
        return (obj.data or {}).get('title', '')

    def get_tags(self, obj):
        return list(PageTag.objects.filter(
            page_group_id=obj.group_id).order_by('tag_name').values_list('tag_name', flat=True))


class PageCreateSerializer(serializers.Serializer):
    section = serializers.SlugRelatedField(
        slug_field='name', queryset=Section.objects.all())
    content = serializers.CharField()
    title = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True, default='published')
    data = serializers.JSONField(required=False, default=dict)
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )


class PageVersionCreateSerializer(serializers.Serializer):
    section = serializers.SlugRelatedField(
        slug_field='name', queryset=Section.objects.all(), required=False)
    content = serializers.CharField()
    title = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    data = serializers.JSONField(required=False, default=dict)
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
    )


class CommentSerializer(serializers.ModelSerializer):
    author_username = serializers.CharField(source='author.username', read_only=True)

    class Meta:
        model = Comment
        fields = [
            'id', 'page', 'author', 'author_username', 'content', 'data',
            'created_at', 'modified_at',
        ]
        read_only_fields = [
            'id', 'page', 'author', 'author_username', 'created_at', 'modified_at',
        ]


class CommentCreateSerializer(serializers.Serializer):
    content = serializers.CharField()
    data = serializers.JSONField(required=False, default=dict)


class PageTagsUpdateSerializer(serializers.Serializer):
    tags = serializers.ListField(child=serializers.CharField(allow_blank=False))


class SectionCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    data = serializers.JSONField(required=False, default=dict)


class SystemPromptSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemPrompt
        fields = [
            'id', 'group_id', 'version', 'is_current', 'name', 'content',
            'data', 'created_at', 'modified_at',
        ]


class SystemPromptCreateSerializer(serializers.Serializer):
    """POST /api/v1/system-prompts/ — new group, or new version when
    group_id references an existing group."""
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    content = serializers.CharField()
    group_id = serializers.UUIDField(required=False, allow_null=True)
    data = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        if not attrs.get('group_id') and not (attrs.get('name') or '').strip():
            raise serializers.ValidationError(
                {'name': 'name is required when creating a new system prompt group.'})
        return attrs


class JobDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobDefinition
        fields = ['id', 'name', 'description', 'status', 'created_at']


class JobDefinitionDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobDefinition
        fields = [
            'id', 'name', 'description', 'status', 'data',
            'last_run_at', 'next_run_at', 'created_at', 'modified_at',
        ]


class JobDefinitionWriteSerializer(serializers.Serializer):
    """POST/PATCH payload for JobDefinitions.

    `data` carries the worker contract keys (model, effort, mcp_tools,
    system_prompt_group_id, timeout_s). Known keys are validated; unknown
    keys pass through. On PATCH the caller's `data` is merged key-by-key
    into the stored data, and an explicit JSON null removes a key.
    """
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=['active', 'paused', 'archived'], required=False)
    data = serializers.JSONField(required=False)

    def validate_data(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('data must be a JSON object.')

        model = value.get('model')
        if model is not None:
            valid_models = {m[0] for m in MODEL_CHOICES}
            if model not in valid_models:
                raise serializers.ValidationError(
                    f'unknown model "{model}"; valid: {", ".join(sorted(valid_models))}.')

        effort = value.get('effort')
        if effort is not None and not isinstance(effort, str):
            raise serializers.ValidationError('effort must be a string.')

        mcp_tools = value.get('mcp_tools')
        if mcp_tools is not None:
            if not isinstance(mcp_tools, list):
                raise serializers.ValidationError('mcp_tools must be a list of server keys.')
            known = set(MCP_SERVERS) | set(REMOTE_EXTRA_MCP_LABELS)
            unknown = [t for t in mcp_tools if t not in known]
            if unknown:
                raise serializers.ValidationError(
                    f'unknown mcp_tools {unknown}; valid: {", ".join(sorted(known))}.')

        sp_group = value.get('system_prompt_group_id')
        if sp_group is not None:
            try:
                sp_uuid = uuid_lib.UUID(str(sp_group))
            except (ValueError, AttributeError, TypeError):
                raise serializers.ValidationError(
                    'system_prompt_group_id must be a UUID.')
            if not SystemPrompt.objects.filter(group_id=sp_uuid, is_current=True).exists():
                raise serializers.ValidationError(
                    f'no system prompt group {sp_group}.')

        timeout_s = value.get('timeout_s')
        if timeout_s is not None and (
                isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0):
            raise serializers.ValidationError('timeout_s must be a positive integer.')

        return value


class JobDetailSerializer(serializers.ModelSerializer):
    result_page_group_id = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            'id', 'definition', 'prompt', 'status', 'error',
            'result_page_group_id', 'data', 'created_at', 'modified_at',
        ]

    def get_result_page_group_id(self, obj):
        return obj.data.get('result_page_group_id') or obj.data.get('result_page_id')

    def get_error(self, obj):
        return obj.data.get('error')


class PromptCreateSerializer(serializers.Serializer):
    section = serializers.SlugRelatedField(
        slug_field='name', queryset=Section.objects.all())
    content = serializers.CharField()
    definition_id = serializers.UUIDField(required=False, allow_null=True)


class JobCreateSerializer(serializers.Serializer):
    prompt_group_id = serializers.UUIDField()
    definition_id = serializers.UUIDField(required=False, allow_null=True)


class JobNotificationSubscriptionSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = JobNotificationSubscription
        fields = [
            'id', 'name', 'callback_url', 'status', 'created_by',
            'created_by_username', 'data', 'created_at', 'modified_at',
        ]
        read_only_fields = [
            'id', 'created_by', 'created_by_username', 'created_at', 'modified_at',
        ]

    def validate_callback_url(self, value):
        parsed = urlparse(value)
        if parsed.scheme.lower() != 'https':
            raise serializers.ValidationError('callback_url must use https.')
        return value

    def validate_status(self, value):
        allowed = {'active', 'paused', 'archived'}
        if value not in allowed:
            raise serializers.ValidationError(
                f'status must be one of: {", ".join(sorted(allowed))}.')
        return value
