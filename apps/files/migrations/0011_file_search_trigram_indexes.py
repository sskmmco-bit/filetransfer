"""Enable pg_trgm + trigram GIN indexes for fast file search.

The file-search backend (apps/files/views._apply_search) filters with
ILIKE '%term%' on `original_filename` and `title` and ranks results by
trigram similarity. A plain B-tree can't accelerate a leading-wildcard
ILIKE, so we add `gin_trgm_ops` GIN indexes — these turn the substring
scan into an index lookup and also back the TrigramSimilarity ranking.
"""
import django.contrib.postgres.indexes
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('files', '0010_storedfile_deleted_by_alter_storedfile_status'),
    ]

    operations = [
        # CREATE EXTENSION IF NOT EXISTS pg_trgm — must run before the indexes.
        TrigramExtension(),
        migrations.AddIndex(
            model_name='storedfile',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['original_filename'],
                name='file_fname_trgm',
                opclasses=['gin_trgm_ops'],
            ),
        ),
        migrations.AddIndex(
            model_name='storedfile',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['title'],
                name='file_title_trgm',
                opclasses=['gin_trgm_ops'],
            ),
        ),
    ]
