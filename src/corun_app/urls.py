from django.urls import path
from . import views

app_name = 'corun'

urlpatterns = [
    path('', views.home, name='home'),
]
