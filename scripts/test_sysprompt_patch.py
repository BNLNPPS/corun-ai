"""Test the codoc-ai sysprompt patch endpoint and its helpers.

Runs against the live prod DB. Creates a throwaway test sysprompt,
exercises every supported op + error code, cleans up. No pytest.

Usage:
    cd /var/www/corun-ai/src && set -a && source .env && set +a && \
        export DJANGO_SETTINGS_MODULE=corun_project.settings && \
        /var/www/corun-ai/.venv/bin/python \
            /home/admin/github/corun-ai/scripts/test_sysprompt_patch.py
"""
import json
import os
import sys
import uuid
from pathlib import Path

# Bootstrap Django: prefer prod src dir (where settings + .env live).
for src_dir in (Path('/var/www/corun-ai/src'), Path(__file__).resolve().parent.parent / 'src'):
    if (src_dir / 'corun_project' / 'settings.py').exists():
        sys.path.insert(0, str(src_dir))
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
        # Source .env if present
        env_file = src_dir / '.env'
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break

import django
django.setup()

from django.contrib.auth import get_user_model
from django.test import Client
from django.db import transaction
from django.db.models import Max

from corun_app.models import SystemPrompt
from codoc_app.views import _sp_find_section


PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def make_sp(content, name=None):
    """Create a throwaway sysprompt; return the row."""
    return SystemPrompt.objects.create(
        group_id=uuid.uuid4(),
        version=1,
        is_current=True,
        name=name or f"surgical-test-{uuid.uuid4().hex[:8]}",
        content=content,
        data={'description': 'test', 'created_by': 'test'},
    )


def cleanup(group_id):
    SystemPrompt.objects.filter(group_id=group_id).delete()


# ─────────────────────────────────────────────────────────────────────
print("\n[group] _sp_find_section helper")
# ─────────────────────────────────────────────────────────────────────

DOC = """# Title

intro

## Foo

old foo body

## Bar

bar body

## Foo

second foo body
"""

r = _sp_find_section(DOC, "Bar")
check("found unique heading", r[0] == 'found' and r[4] == 1, str(r))

r = _sp_find_section(DOC, "Nope")
check("not_found on absent heading", r == ('not_found', 0), str(r))

r = _sp_find_section(DOC, "Foo")
check("multiple on duplicate without occurrence", r == ('multiple', 2), str(r))

r = _sp_find_section(DOC, "Foo", occurrence=1)
check("occurrence=1 picks first Foo", r[0] == 'found', str(r))

r = _sp_find_section(DOC, "Foo", occurrence=2)
check("occurrence=2 picks second Foo", r[0] == 'found', str(r))

r = _sp_find_section(DOC, "Foo", occurrence=99)
check("out_of_range on too-high occurrence", r[0] == 'out_of_range', str(r))

# Fenced code block — heading inside ``` should be ignored
DOC_FENCE = "## A\nbody A\n```\n## B\nfake\n```\n## C\nbody C\n"
r = _sp_find_section(DOC_FENCE, "B")
check("not_found when only match is inside ``` fence", r == ('not_found', 0), str(r))


# ─────────────────────────────────────────────────────────────────────
print("\n[group] HTTP endpoint via Django test client")
# ─────────────────────────────────────────────────────────────────────

User = get_user_model()
test_user = User.objects.filter(is_active=True).first()
if not test_user:
    print("  SKIP: no active user found in DB")
    sys.exit(0 if FAIL == 0 else 1)

client = Client(HTTP_HOST='localhost')
client.force_login(test_user)
print(f"  (using user: {test_user.username})")


def post_patch(group_id, payload, expect_status=200):
    # codoc_app urls are mounted at '' in project urls.py; the /doc/ prefix
    # is added by Apache's WSGI subpath mount, not by Django itself.
    url = f"/_api/sysprompt/{group_id}/patch/"
    resp = client.post(url, data=json.dumps(payload), content_type='application/json')
    if resp.status_code >= 500 or 'application/json' not in resp.get('Content-Type', ''):
        print(f"  !! non-JSON response status={resp.status_code} ct={resp.get('Content-Type')}")
        body = resp.content.decode('utf-8', errors='replace')[:400]
        print(f"  !! body[:400]: {body}")
    return resp


# 1. Happy path replace_text
sp = make_sp("alpha beta gamma\nfoo bar baz quux\nend.")
resp = post_patch(sp.group_id, {"op": "replace_text",
                                "old_text": "bar baz", "new_text": "BAR-BAZ"})
data = resp.json()
check("replace_text 200", resp.status_code == 200 and data.get('ok'), str(data))
check("replace_text creates new version",
      data.get('version') == 2, f"version={data.get('version')}")
new_current = SystemPrompt.objects.get(group_id=sp.group_id, is_current=True)
check("replace_text content updated in DB",
      'BAR-BAZ' in new_current.content and 'bar baz' not in new_current.content,
      new_current.content[:120])
check("replaced_count=1", data.get('replaced_count') == 1, str(data))
cleanup(sp.group_id)

# 2. NO_MATCH
sp = make_sp("alpha beta gamma")
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "MISSING", "new_text": "X"})
data = resp.json()
check("NO_MATCH returns 404 + code",
      resp.status_code == 404 and data.get('code') == 'NO_MATCH', str(data))
cleanup(sp.group_id)

# 3. MULTIPLE_MATCHES (without replace_all)
sp = make_sp("foo and foo and foo")
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "foo", "new_text": "BAR"})
data = resp.json()
check("MULTIPLE_MATCHES returns 409 + count",
      resp.status_code == 409 and data.get('code') == 'MULTIPLE_MATCHES'
      and data.get('count') == 3, str(data))
cleanup(sp.group_id)

# 4. replace_all=True
sp = make_sp("foo and foo and foo")
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "foo", "new_text": "BAR",
                                "replace_all": True})
data = resp.json()
new_current = SystemPrompt.objects.get(group_id=sp.group_id, is_current=True)
check("replace_all replaces all + returns count",
      data.get('replaced_count') == 3 and new_current.content == "BAR and BAR and BAR",
      str(data))
cleanup(sp.group_id)

# 5. STALE_PRECONDITION
sp = make_sp("alpha beta gamma\nfoo bar baz")
mod = sp.modified_at.isoformat()
# Bump it
post_patch(sp.group_id, {"op": "replace_text", "old_text": "alpha", "new_text": "ALPHA"})
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "beta", "new_text": "BETA",
                                "expected_modified_at": mod})
data = resp.json()
check("STALE_PRECONDITION returns 409 + code",
      resp.status_code == 409 and data.get('code') == 'STALE_PRECONDITION', str(data))
cleanup(sp.group_id)

# 5b. NOOP_PATCH on identical old/new (no silent succeed)
sp = make_sp("alpha beta gamma")
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "beta", "new_text": "beta"})
data = resp.json()
check("NOOP_PATCH returns 409 when old_text == new_text",
      resp.status_code == 409 and data.get('code') == 'NOOP_PATCH', str(data))
cleanup(sp.group_id)

# 6. Happy path replace_section
sp = make_sp(DOC)
resp = post_patch(sp.group_id, {"op": "replace_section", "heading": "Bar",
                                "new_body": "\nNEW BAR BODY\n"})
data = resp.json()
new_current = SystemPrompt.objects.get(group_id=sp.group_id, is_current=True)
check("replace_section 200 + heading preserved",
      resp.status_code == 200 and "## Bar" in new_current.content
      and "NEW BAR BODY" in new_current.content
      and "bar body" not in new_current.content,
      str(data))
cleanup(sp.group_id)

# 7. MULTIPLE_HEADINGS without occurrence
sp = make_sp(DOC)
resp = post_patch(sp.group_id, {"op": "replace_section", "heading": "Foo", "new_body": "x"})
data = resp.json()
check("MULTIPLE_HEADINGS returns 409 + count",
      resp.status_code == 409 and data.get('code') == 'MULTIPLE_HEADINGS'
      and data.get('count') == 2, str(data))
cleanup(sp.group_id)

# 8. occurrence picks the right one
sp = make_sp(DOC)
resp = post_patch(sp.group_id, {"op": "replace_section", "heading": "Foo",
                                "new_body": "SECOND-FOO", "occurrence": 2})
new_current = SystemPrompt.objects.get(group_id=sp.group_id, is_current=True)
check("occurrence=2 replaces second Foo",
      resp.status_code == 200 and "SECOND-FOO" in new_current.content
      and "old foo body" in new_current.content,
      new_current.content[-200:])
cleanup(sp.group_id)

# 9. HEADING_NOT_FOUND
sp = make_sp(DOC)
resp = post_patch(sp.group_id, {"op": "replace_section", "heading": "Nope", "new_body": "x"})
data = resp.json()
check("HEADING_NOT_FOUND returns 404",
      resp.status_code == 404 and data.get('code') == 'HEADING_NOT_FOUND', str(data))
cleanup(sp.group_id)

# 9b. NOOP_PATCH when section new_body matches existing body
sp = make_sp(DOC)
existing_bar_body = "\nbar body\n"  # body of "## Bar" in DOC, including blank-line padding
resp = post_patch(sp.group_id, {"op": "replace_section", "heading": "Bar",
                                "new_body": existing_bar_body})
data = resp.json()
check("section NOOP_PATCH returns 409",
      resp.status_code == 409 and data.get('code') == 'NOOP_PATCH', str(data))
cleanup(sp.group_id)

# 10. BAD_REQUEST on bad op
sp = make_sp("anything")
resp = post_patch(sp.group_id, {"op": "fake_op"})
data = resp.json()
check("BAD_REQUEST on unknown op",
      resp.status_code == 400 and data.get('code') == 'BAD_REQUEST', str(data))
cleanup(sp.group_id)

# 11. NOT_FOUND on missing group
fake_gid = uuid.uuid4()
resp = post_patch(fake_gid, {"op": "replace_text", "old_text": "x", "new_text": "y"})
data = resp.json()
check("NOT_FOUND on missing group_id",
      resp.status_code == 404 and data.get('code') == 'NOT_FOUND', str(data))

# 12. Version-numbering robustness — survives a v-collision in history
sp = make_sp("v1 content")
# Manually create a v2 (simulating a rolled-back-but-not-deleted version, like
# what happened to codoc-pr-review with v11). Mark not-current.
SystemPrompt.objects.create(
    group_id=sp.group_id, version=2, is_current=False,
    name=sp.name, content="v2 not-current",
    data={'description': 'rolled back'},
)
# Now patch — should write v3, not collide on v2
resp = post_patch(sp.group_id, {"op": "replace_text", "old_text": "v1", "new_text": "PATCHED"})
data = resp.json()
check("survives version-number hole (writes max+1, not current+1)",
      resp.status_code == 200 and data.get('version') == 3, str(data))
cleanup(sp.group_id)


# ─────────────────────────────────────────────────────────────────────
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
