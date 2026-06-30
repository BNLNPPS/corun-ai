"""
DRF API views for the corun-ai REST API.

Authentication: Token (Authorization: Token <token>)
Authorization: IsAuthenticated — any valid token may call any endpoint.
"""

import uuid

import markdown as md_lib
from django.db import transaction
from django.db.models import Max, Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from corun_app.models import Comment, Job, JobDefinition, Page, PageTag, Prompt, Section
from corun_app.models import JobNotificationSubscription

from .serializers import (
    CommentCreateSerializer,
    CommentSerializer,
    JobCreateSerializer,
    JobDefinitionSerializer,
    JobDetailSerializer,
    JobNotificationSubscriptionSerializer,
    PageCreateSerializer,
    PageDetailSerializer,
    PageTagsUpdateSerializer,
    PageVersionCreateSerializer,
    PromptCreateSerializer,
    PromptDetailSerializer,
    SectionDetailSerializer,
    SectionSerializer,
)


def _render_markdown(content):
    return md_lib.markdown(content, extensions=['fenced_code', 'tables', 'toc'])


def _normalize_tag_list(raw_tags):
    tags = []
    seen = set()
    for raw in raw_tags or []:
        tag = str(raw).strip().lstrip(':')
        if not tag:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _set_page_tags(page_group_id, tags):
    current = set(PageTag.objects.filter(
        page_group_id=page_group_id).values_list('tag_name', flat=True))
    desired = set(_normalize_tag_list(tags))
    for tag_name in sorted(desired - current):
        PageTag.objects.create(page_group_id=page_group_id, tag_name=tag_name)
    for tag_name in current - desired:
        PageTag.objects.filter(page_group_id=page_group_id, tag_name=tag_name).delete()


def _with_title(data, title):
    payload = dict(data or {})
    if title is not None:
        payload['title'] = title
    return payload


def _parse_bool(value):
    if value is None or value == '':
        return None
    value = str(value).strip().lower()
    if value in ('1', 'true', 'yes', 'y'):
        return True
    if value in ('0', 'false', 'no', 'n'):
        return False
    if value in ('all', '*'):
        return None
    raise ValueError('expected true, false, or all')


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

class PageListCreateView(APIView):
    """GET/POST /api/v1/pages/ — list or create Page documents."""

    def get(self, request):
        qs = Page.objects.select_related('section').order_by('-created_at')

        current = request.query_params.get('current')
        if current is None:
            qs = qs.filter(is_current=True)
        else:
            try:
                parsed_current = _parse_bool(current)
            except ValueError as e:
                return Response({'current': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            if parsed_current is not None:
                qs = qs.filter(is_current=parsed_current)

        section = (request.query_params.get('section') or '').strip()
        if section:
            qs = qs.filter(section__name=section)

        for key in ('artifact_type', 'source_system', 'subject_type', 'subject_key'):
            value = (request.query_params.get(key) or '').strip()
            if value:
                qs = qs.filter(**{f'data__{key}': value})

        for tag in _normalize_tag_list(request.query_params.getlist('tag')):
            tagged_groups = PageTag.objects.filter(
                tag_name=tag).values_list('page_group_id', flat=True)
            qs = qs.filter(group_id__in=tagged_groups)

        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(content__icontains=q)
                | Q(data__title__icontains=q)
                | Q(data__subject_label__icontains=q)
                | Q(data__subject_key__icontains=q)
            )

        try:
            limit = min(max(int(request.query_params.get('limit', 100)), 1), 500)
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            return Response(
                {'detail': 'limit and offset must be integers.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        count = qs.count()
        items = qs[offset:offset + limit]
        return Response({
            'count': count,
            'limit': limit,
            'offset': offset,
            'items': PageDetailSerializer(items, many=True).data,
        })

    def post(self, request):
        ser = PageCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        content = ser.validated_data['content']
        page = Page.objects.create(
            group_id=uuid.uuid4(),
            version=1,
            is_current=True,
            prompt=None,
            section=ser.validated_data['section'],
            content=content,
            content_rendered=_render_markdown(content),
            status=ser.validated_data.get('status') or 'published',
            data=_with_title(
                ser.validated_data.get('data') or {},
                ser.validated_data.get('title'),
            ),
        )
        _set_page_tags(page.group_id, ser.validated_data.get('tags') or [])
        return Response(PageDetailSerializer(page).data, status=status.HTTP_201_CREATED)


class PageDetailView(APIView):
    """GET /api/v1/pages/<group_id>/ — current version of a page group."""

    def get(self, request, group_id):
        try:
            page = Page.objects.get(group_id=group_id, is_current=True)
        except Page.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PageDetailSerializer(page).data)


class PageVersionListCreateView(APIView):
    """GET/POST /api/v1/pages/<group_id>/versions/."""

    def get(self, request, group_id):
        versions = Page.objects.filter(
            group_id=group_id).select_related('section').order_by('-version')
        if not versions.exists():
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PageDetailSerializer(versions, many=True).data)

    def post(self, request, group_id):
        ser = PageVersionCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            current = Page.objects.select_for_update().filter(
                group_id=group_id, is_current=True).first()
            if current is None:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

            current.is_current = False
            current.save(update_fields=['is_current', 'modified_at'])

            next_version = (
                Page.objects.filter(group_id=group_id).aggregate(m=Max('version'))['m'] or 0
            ) + 1
            content = ser.validated_data['content']
            page = Page.objects.create(
                group_id=current.group_id,
                version=next_version,
                is_current=True,
                prompt=current.prompt,
                section=ser.validated_data.get('section') or current.section,
                content=content,
                content_rendered=_render_markdown(content),
                status=ser.validated_data.get('status') or current.status,
                data=_with_title(
                    ser.validated_data.get('data') or {},
                    ser.validated_data.get('title'),
                ),
            )

            if 'tags' in ser.validated_data:
                _set_page_tags(page.group_id, ser.validated_data.get('tags') or [])

        return Response(PageDetailSerializer(page).data, status=status.HTTP_201_CREATED)


class PageVersionDetailView(APIView):
    """GET /api/v1/pages/<group_id>/versions/<version>/."""

    def get(self, request, group_id, version):
        try:
            page = Page.objects.select_related('section').get(
                group_id=group_id, version=version)
        except Page.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PageDetailSerializer(page).data)


class PageCommentListCreateView(APIView):
    """GET/POST /api/v1/pages/<group_id>/comments/."""

    def get_current_page(self, group_id):
        return Page.objects.filter(group_id=group_id, is_current=True).first()

    def get(self, request, group_id):
        page = self.get_current_page(group_id)
        if page is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        comments = Comment.objects.filter(page=page).select_related('author').order_by('created_at')
        return Response(CommentSerializer(comments, many=True).data)

    def post(self, request, group_id):
        page = self.get_current_page(group_id)
        if page is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        ser = CommentCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        comment = Comment.objects.create(
            page=page,
            author=request.user,
            content=ser.validated_data['content'],
            data=ser.validated_data.get('data') or {},
        )
        return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)


class CommentDetailView(APIView):
    """DELETE /api/v1/comments/<comment_id>/."""

    def delete(self, request, comment_id):
        try:
            comment = Comment.objects.get(id=comment_id)
        except Comment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not (
            request.user.is_staff
            or request.user.is_superuser
            or comment.author_id == request.user.id
        ):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        comment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PageTagsUpdateView(APIView):
    """PATCH /api/v1/pages/<group_id>/tags/ — replace group-level PageTag rows."""

    def patch(self, request, group_id):
        if not Page.objects.filter(group_id=group_id, is_current=True).exists():
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = PageTagsUpdateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        tags = _normalize_tag_list(ser.validated_data['tags'])
        _set_page_tags(group_id, tags)
        page = Page.objects.get(group_id=group_id, is_current=True)
        return Response({
            'ok': True,
            'group_id': str(group_id),
            'tags': PageDetailSerializer(page).data['tags'],
        })


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
