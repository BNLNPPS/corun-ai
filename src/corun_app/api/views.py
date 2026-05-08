"""
DRF API views for the corun-ai REST API.

Authentication: Token (Authorization: Token <token>)
Authorization: IsAuthenticated — any valid token may call any endpoint.
"""

import uuid

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from corun_app.models import Job, JobDefinition, Page, Prompt, Section
from corun_app.models import JobNotificationSubscription

from .serializers import (
    JobCreateSerializer,
    JobDefinitionSerializer,
    JobDetailSerializer,
    JobNotificationSubscriptionSerializer,
    PageDetailSerializer,
    PromptCreateSerializer,
    PromptDetailSerializer,
    SectionDetailSerializer,
    SectionSerializer,
)


# ── Sections ──────────────────────────────────────────────────────────────────

class SectionListView(APIView):
    """GET /api/v1/sections/ — list active sections."""

    def get(self, request):
        sections = Section.objects.filter(status='active').order_by('data__sort_order', 'name')
        return Response(SectionSerializer(sections, many=True).data)


class SectionDetailView(APIView):
    """GET /api/v1/sections/<name>/ — section detail with current prompts."""

    def get(self, request, name):
        try:
            section = Section.objects.get(name=name, status='active')
        except Section.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(SectionDetailSerializer(section).data)


# ── Prompts ───────────────────────────────────────────────────────────────────

class PromptDetailView(APIView):
    """GET /api/v1/prompts/<group_id>/ — current version of a prompt group."""

    def get(self, request, group_id):
        try:
            prompt = Prompt.objects.get(group_id=group_id, is_current=True)
        except Prompt.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PromptDetailSerializer(prompt).data)


class PromptCreateView(APIView):
    """POST /api/v1/prompts/ — create a new prompt."""

    def post(self, request):
        ser = PromptCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        section = ser.validated_data['section']
        content = ser.validated_data['content']
        group_id = uuid.uuid4()

        definition_id = ser.validated_data.get('definition_id')

        prompt = Prompt.objects.create(
            group_id=group_id,
            version=1,
            is_current=True,
            section=section,
            content=content,
            submitted_by=request.user,
            status='pending',
            data={'definition_id': str(definition_id)} if definition_id else {},
        )
        return Response(PromptDetailSerializer(prompt).data, status=status.HTTP_201_CREATED)


# ── Pages ─────────────────────────────────────────────────────────────────────

class PageDetailView(APIView):
    """GET /api/v1/pages/<group_id>/ — current version of a page group."""

    def get(self, request, group_id):
        try:
            page = Page.objects.get(group_id=group_id, is_current=True)
        except Page.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PageDetailSerializer(page).data)


# ── Jobs ──────────────────────────────────────────────────────────────────────

class JobDetailView(APIView):
    """GET /api/v1/jobs/<job_id>/ — job status and result."""

    def get(self, request, job_id):
        try:
            job = Job.objects.get(id=job_id)
        except Job.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(JobDetailSerializer(job).data)


class JobCreateView(APIView):
    """POST /api/v1/jobs/ — submit a generation job."""

    def post(self, request):
        ser = JobCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        prompt_group_id = ser.validated_data['prompt_group_id']
        definition_id = ser.validated_data.get('definition_id')

        try:
            prompt = Prompt.objects.get(group_id=prompt_group_id, is_current=True)
        except Prompt.DoesNotExist:
            return Response(
                {'prompt_group_id': 'No current prompt with this group_id.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve definition: (1) explicit, (2) stored on prompt, (3) default.
        definition = None
        if definition_id:
            definition = JobDefinition.objects.filter(id=definition_id, status='active').first()
            if definition is None:
                return Response(
                    {'definition_id': 'No active JobDefinition with this id.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if definition is None:
            stored_id = prompt.data.get('definition_id')
            if stored_id:
                definition = JobDefinition.objects.filter(id=stored_id, status='active').first()
        if definition is None:
            from codoc_app.generate import get_or_create_default_def
            definition = get_or_create_default_def()

        from codoc_app.generate import start_generation
        job = start_generation(prompt, definition, triggered_by=request.user)
        return Response(JobDetailSerializer(job).data, status=status.HTTP_201_CREATED)


class JobAbortView(APIView):
    """POST /api/v1/jobs/<job_id>/abort/ — cancel a running or queued job."""

    def post(self, request, job_id):
        try:
            job = Job.objects.get(id=job_id)
        except Job.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        if job.status not in ('queued', 'running'):
            return Response(
                {'detail': f'Job cannot be aborted in status "{job.status}".'},
                status=status.HTTP_409_CONFLICT,
            )

        job.status = 'cancelled'
        job.data = {**job.data, 'error': 'Aborted by user'}
        job.save(update_fields=['status', 'data', 'modified_at'])

        if job.prompt:
            job.prompt.status = 'saved'
            job.prompt.save(update_fields=['status', 'modified_at'])
        return Response(JobDetailSerializer(job).data)


# ── JobDefinitions ────────────────────────────────────────────────────────────

class JobDefinitionListView(APIView):
    """GET /api/v1/definitions/ — list active job definitions."""

    def get(self, request):
        definitions = JobDefinition.objects.filter(status='active').order_by('name')
        return Response(JobDefinitionSerializer(definitions, many=True).data)


# ── Job Notification Subscriptions ───────────────────────────────────────────

class JobNotificationSubscriptionListView(APIView):
    """GET/POST /api/v1/notification-subscriptions/."""

    def get_queryset(self, request):
        qs = JobNotificationSubscription.objects.exclude(status='archived')
        if request.user.is_staff:
            return qs
        return qs.filter(created_by=request.user)

    def get(self, request):
        subscriptions = self.get_queryset(request).order_by('name')
        return Response(JobNotificationSubscriptionSerializer(subscriptions, many=True).data)

    def post(self, request):
        ser = JobNotificationSubscriptionSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        subscription = ser.save(created_by=request.user)
        return Response(
            JobNotificationSubscriptionSerializer(subscription).data,
            status=status.HTTP_201_CREATED,
        )


class JobNotificationSubscriptionDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/notification-subscriptions/<subscription_id>/."""

    def get_object(self, request, subscription_id):
        try:
            subscription = JobNotificationSubscription.objects.get(id=subscription_id)
        except JobNotificationSubscription.DoesNotExist:
            return None
        if request.user.is_staff or subscription.created_by_id == request.user.id:
            return subscription
        return None

    def get(self, request, subscription_id):
        subscription = self.get_object(request, subscription_id)
        if subscription is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(JobNotificationSubscriptionSerializer(subscription).data)

    def patch(self, request, subscription_id):
        subscription = self.get_object(request, subscription_id)
        if subscription is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        ser = JobNotificationSubscriptionSerializer(
            subscription, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        subscription = ser.save()
        return Response(JobNotificationSubscriptionSerializer(subscription).data)

    def delete(self, request, subscription_id):
        subscription = self.get_object(request, subscription_id)
        if subscription is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        subscription.status = 'archived'
        subscription.save(update_fields=['status', 'modified_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)
