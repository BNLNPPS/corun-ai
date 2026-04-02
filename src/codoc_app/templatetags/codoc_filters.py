from django import template

register = template.Library()


@register.filter
def duration(seconds):
    """Format seconds as 'Xm Ys'. No fractional seconds."""
    try:
        s = int(float(seconds))
    except (ValueError, TypeError):
        return ''
    if s < 60:
        return f'{s}s'
    m = s // 60
    s = s % 60
    return f'{m}m {s}s'
