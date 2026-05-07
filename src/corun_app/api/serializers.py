"""
DRF serializers for the corun-ai REST API.
"""

from rest_framework import serializers

from corun_app.models import Job, JobDefinition, Page, Prompt, Section


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

    class Meta:
        model = Page
        fields = [
            'id', 'group_id', 'version', 'is_current',
            'section', 'section_name', 'content', 'content_rendered',
            'status', 'data', 'created_at', 'modified_at',
        ]


class JobDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobDefinition
        fields = ['id', 'name', 'description', 'status', 'created_at']


class JobDetailSerializer(serializers.ModelSerializer):
    result_page_group_id = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            'id', 'definition', 'prompt', 'status',
            'result_page_group_id', 'data', 'created_at', 'modified_at',
        ]

    def get_result_page_group_id(self, obj):
        return obj.data.get('result_page_group_id') or obj.data.get('result_page_id')


class PromptCreateSerializer(serializers.Serializer):
    section = serializers.SlugRelatedField(
        slug_field='name', queryset=Section.objects.all())
    content = serializers.CharField()
    definition_id = serializers.UUIDField(required=False, allow_null=True)


class JobCreateSerializer(serializers.Serializer):
    prompt_group_id = serializers.UUIDField()
    definition_id = serializers.UUIDField(required=False, allow_null=True)
