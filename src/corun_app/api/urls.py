from django.urls import path

from .views import (
    CommentDetailView,
    JobAbortView,
    JobCreateView,
    JobDefinitionDetailView,
    JobDefinitionListView,
    JobDetailView,
    JobLogView,
    JobNotificationSubscriptionDetailView,
    JobNotificationSubscriptionListView,
    PageCommentListCreateView,
    PageDetailView,
    PageListCreateView,
    PageTagsUpdateView,
    PageVersionDetailView,
    PageVersionListCreateView,
    PromptCreateView,
    PromptDetailView,
    SectionDetailView,
    SectionListView,
    SystemPromptDetailView,
    SystemPromptListCreateView,
)

app_name = 'api'

urlpatterns = [
    # Sections
    path('v1/sections/', SectionListView.as_view(), name='section-list'),
    path('v1/sections/<str:name>/', SectionDetailView.as_view(), name='section-detail'),

    # Prompts
    path('v1/prompts/', PromptCreateView.as_view(), name='prompt-create'),
    path('v1/prompts/<uuid:group_id>/', PromptDetailView.as_view(), name='prompt-detail'),

    # Pages
    path('v1/pages/', PageListCreateView.as_view(), name='page-list-create'),
    path('v1/pages/<uuid:group_id>/', PageDetailView.as_view(), name='page-detail'),
    path(
        'v1/pages/<uuid:group_id>/versions/',
        PageVersionListCreateView.as_view(),
        name='page-version-list-create',
    ),
    path(
        'v1/pages/<uuid:group_id>/versions/<int:version>/',
        PageVersionDetailView.as_view(),
        name='page-version-detail',
    ),
    path(
        'v1/pages/<uuid:group_id>/comments/',
        PageCommentListCreateView.as_view(),
        name='page-comment-list-create',
    ),
    path(
        'v1/pages/<uuid:group_id>/tags/',
        PageTagsUpdateView.as_view(),
        name='page-tags-update',
    ),
    path('v1/comments/<uuid:comment_id>/', CommentDetailView.as_view(), name='comment-detail'),

    # System prompts
    path('v1/system-prompts/', SystemPromptListCreateView.as_view(),
         name='system-prompt-list-create'),
    path('v1/system-prompts/<uuid:group_id>/', SystemPromptDetailView.as_view(),
         name='system-prompt-detail'),

    # Jobs
    path('v1/jobs/', JobCreateView.as_view(), name='job-create'),
    path('v1/jobs/<uuid:job_id>/', JobDetailView.as_view(), name='job-detail'),
    path('v1/jobs/<uuid:job_id>/abort/', JobAbortView.as_view(), name='job-abort'),
    path('v1/jobs/<uuid:job_id>/log/', JobLogView.as_view(), name='job-log'),

    # JobDefinitions
    path('v1/definitions/', JobDefinitionListView.as_view(), name='definition-list'),
    path('v1/definitions/<uuid:definition_id>/', JobDefinitionDetailView.as_view(),
         name='definition-detail'),

    # Job notification subscriptions
    path(
        'v1/notification-subscriptions/',
        JobNotificationSubscriptionListView.as_view(),
        name='notification-subscription-list',
    ),
    path(
        'v1/notification-subscriptions/<uuid:subscription_id>/',
        JobNotificationSubscriptionDetailView.as_view(),
        name='notification-subscription-detail',
    ),
]
