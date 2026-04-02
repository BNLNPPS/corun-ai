"""Template context processors for codoc_app."""

from corun_app.models import UserProfile


def user_theme(request):
    """Add user's theme preference to template context."""
    theme = 'dark'
    if hasattr(request, 'user') and request.user.is_authenticated:
        try:
            theme = request.user.profile.theme
        except UserProfile.DoesNotExist:
            pass
    return {'user_theme': theme}
