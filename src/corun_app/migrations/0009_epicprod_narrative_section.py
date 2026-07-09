from django.db import migrations


def create_epicprod_narrative_section(apps, schema_editor):
    Section = apps.get_model('corun_app', 'Section')
    section, created = Section.objects.get_or_create(
        name='epicprod.narrative',
        defaults={
            'title': 'ePIC Production Narrative',
            'description': 'Narrative summaries for ePIC production monitoring integrations.',
            'status': 'active',
            'data': {
                'purpose': 'epicprod narrative Pages',
                'ui_visible': False,
                'integration': 'epicprod',
            },
        },
    )
    if not created:
        data = dict(section.data or {})
        changed = False
        for key, value in {
            'purpose': 'epicprod narrative Pages',
            'ui_visible': False,
            'integration': 'epicprod',
        }.items():
            if data.get(key) != value:
                data[key] = value
                changed = True
        if changed:
            section.data = data
            section.save(update_fields=['data', 'modified_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('corun_app', '0008_pagetag'),
    ]

    operations = [
        migrations.RunPython(create_epicprod_narrative_section, migrations.RunPython.noop),
    ]
