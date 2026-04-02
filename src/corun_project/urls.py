from django.contrib import admin
from django.urls import path, include
from codoc_app.views import account_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('account/', account_view, name='account'),
    path('', include('codoc_app.urls')),
]
