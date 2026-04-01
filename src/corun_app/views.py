from django.http import HttpResponse


def home(request):
    return HttpResponse('<h1>corun-ai</h1><p>Collaborative AI runner. Coming soon.</p>')
