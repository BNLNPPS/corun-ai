from django.urls import path

from .views import (
    JobAbortView,
    JobCreateView,
    JobDefinitionListView,
    JobDetailView,
    PageDetailView,
    PromptCreateView,
    PromptDetailView,
    SectionDetailView,
    SectionListView,
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
    path('v1/pages/<uuid:group_id>/', PageDetailView.as_view(), name='page-detail'),

    # Jobs
    path('v1/jobs/', JobCreateView.as_view(), name='job-create'),
    path('v1/jobs/<uuid:job_id>/', JobDetailView.as_view(), name='job-detail'),
    path('v1/jobs/<uuid:job_id>/abort/', JobAbortView.as_view(), name='job-abort'),

    # JobDefinitions
    path('v1/definitions/', JobDefinitionListView.as_view(), name='definition-list'),
]
