import re

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


_PR_URL_RE = re.compile(r'https?://github\.com/[^/]+/[^/]+/pull/(\d+)')


@register.filter
def prompt_title(content):
    """Render a prompt's content as a navigator-friendly one-line title.

    Handles three formats that have existed for PR-review prompts:
      - New:  "PR #<N>: <title>\\n<url>"  → passes through first line
      - Old:  "PR: <title>\\n<url>"        → rewrites to "PR #<N>: <title>"
      - Oldest: bare PR URL only           → "PR #<N>" (title unknown)
      - Anything else                      → the first line unchanged

    Pure display transform; does not touch stored content.
    """
    if not content:
        return ''
    first = content.split('\n', 1)[0].strip()
    # Already in the desired shape.
    if first.startswith('PR #'):
        return first
    # Try to extract a PR number from the content (usually line 2).
    m = _PR_URL_RE.search(content)
    pr_num = m.group(1) if m else None
    if first.startswith('PR:') and pr_num:
        return f'PR #{pr_num}:{first[3:]}'.rstrip()
    if first.startswith('PR:'):
        return first  # no URL to extract number from
    # Bare URL as the whole content (oldest submissions).
    if _PR_URL_RE.fullmatch(first) and pr_num:
        return f'PR #{pr_num}'
    return first
