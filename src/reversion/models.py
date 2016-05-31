from __future__ import unicode_literals
from django.contrib.contenttypes.models import ContentType
try:
    from django.contrib.contenttypes.fields import GenericForeignKey
except ImportError:  # Django < 1.9 pragma: no cover
    from django.contrib.contenttypes.generic import GenericForeignKey
from django.conf import settings
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, IntegrityError, transaction
from django.db.models.lookups import In
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import force_text, python_2_unicode_compatible
from reversion.errors import RevertError


def safe_revert(versions):
    """
    Attempts to revert the given models contained in the give versions.

    This method will attempt to resolve dependencies between the versions to revert
    them in the correct order to avoid database integrity errors.
    """
    unreverted_versions = []
    for version in versions:
        try:
            with transaction.atomic():
                version.revert()
        except (IntegrityError, ObjectDoesNotExist):  # pragma: no cover
            unreverted_versions.append(version)
    if len(unreverted_versions) == len(versions):  # pragma: no cover
        raise RevertError("Could not revert revision, due to database integrity errors.")
    if unreverted_versions:  # pragma: no cover
        safe_revert(unreverted_versions)


@python_2_unicode_compatible
class Revision(models.Model):

    """A group of related object versions."""

    manager_slug = models.CharField(
        max_length=191,
        db_index=True,
        default="default",
    )

    date_created = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_("date created"),
        help_text="The date and time this revision was created.",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name=_("user"),
        help_text="The user who created this revision.",
    )

    comment = models.TextField(
        blank=True,
        verbose_name=_("comment"),
        help_text="A text comment on this revision.",
    )

    def revert(self, delete=False):
        """Reverts all objects in this revision."""
        version_set = self.version_set.all()
        # Optionally delete objects no longer in the current revision.
        if delete:
            # Get a dict of all objects in this revision.
            old_revision = set()
            for version in version_set:
                try:
                    obj = version.object
                except ContentType.objects.get_for_id(version.content_type_id).model_class().DoesNotExist:
                    pass
                else:
                    old_revision.add(obj)
            # Calculate the set of all objects that are in the revision now.
            from reversion.revisions import RevisionManager
            current_revision = RevisionManager.get_manager(self.manager_slug)._follow_relationships(
                obj
                for obj in old_revision
                if obj is not None
            )
            # Delete objects that are no longer in the current revision.
            for item in current_revision:
                if item not in old_revision:
                    item.delete()
        # Attempt to revert all revisions.
        safe_revert(version_set)

    def __str__(self):
        """Returns a unicode representation."""
        return ", ".join(force_text(version) for version in self.version_set.all())

    class Meta:
        app_label = 'reversion'


class VersionQuerySet(models.QuerySet):

    def get_unique(self):
        """
        Returns a generator of unique version data.
        """
        last_field_dict = None
        for version in self.iterator():
            if last_field_dict != version.local_field_dict:
                yield version
            last_field_dict = version.local_field_dict


@python_2_unicode_compatible
class Version(models.Model):

    """A saved version of a database model."""

    objects = VersionQuerySet.as_manager()

    revision = models.ForeignKey(
        Revision,
        on_delete=models.CASCADE,
        help_text="The revision that contains this version.",
    )

    object_id = models.CharField(
        max_length=191,
        help_text="Primary key of the model under version control.",
    )

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        help_text="Content type of the model under version control.",
    )

    # A link to the current instance, not the version stored in this Version!
    object = GenericForeignKey(
        ct_field="content_type",
        fk_field="object_id",
    )

    format = models.CharField(
        max_length=255,
        help_text="The serialization format used by this model.",
    )

    serialized_data = models.TextField(
        help_text="The serialized form of this version of the model.",
    )

    object_repr = models.TextField(
        help_text="A string representation of the object.",
    )

    @cached_property
    def object_version(self):
        """The stored version of the model."""
        data = self.serialized_data
        data = force_text(data.encode("utf8"))
        return list(serializers.deserialize(self.format, data, ignorenonexistent=True))[0]

    @cached_property
    def local_field_dict(self):
        """
        A dictionary mapping field names to field values in this version
        of the model.

        Parent links of inherited multi-table models will not be followed.
        """
        object_version = self.object_version
        obj = object_version.object
        result = {}
        for field in obj._meta.fields:
            result[field.name] = field.value_from_object(obj)
        result.update(object_version.m2m_data)
        return result

    @cached_property
    def field_dict(self):
        """
        A dictionary mapping field names to field values in this version
        of the model.

        This method will follow parent links, if present.
        """
        object_version = self.object_version
        obj = object_version.object
        result = self.local_field_dict
        # Add parent data.
        for parent_class, field in obj._meta.concrete_model._meta.parents.items():
            field = field or obj.pk
            if obj._meta.proxy and parent_class == obj._meta.concrete_model:
                continue
            content_type = ContentType.objects.get_for_model(parent_class)
            parent_id = getattr(obj, field.attname)
            parent_version = Version.objects.get(
                revision__id=self.revision_id,
                content_type=content_type,
                object_id=parent_id,
            )
            result.update(parent_version.field_dict)
        return result

    def revert(self):
        """Recovers the model in this version."""
        self.object_version.save()

    def __str__(self):
        """Returns a unicode representation."""
        return self.object_repr

    class Meta:
        app_label = 'reversion'
        index_together = (
            ("object_id", "content_type",),
        )


class Str(models.Func):

    """Casts a value to the database's text type."""

    function = "CAST"
    template = "%(function)s(%(expressions)s as %(db_type)s)"

    def __init__(self, expression):
        super(Str, self).__init__(expression, output_field=models.TextField())

    def as_sql(self, compiler, connection):
        self.extra["db_type"] = self.output_field.db_type(connection)
        return super(Str, self).as_sql(compiler, connection)


@models.Field.register_lookup
class ReversionSubqueryLookup(models.Lookup):

    """
    Performs a subquery using an SQL `IN` clause, selecting the bast strategy
    for the database.
    """

    lookup_name = "reversion_in"

    # Strategies.

    def __init__(self, lhs, rhs):
        rhs, self.rhs_field_name = rhs
        rhs = rhs.values_list(self.rhs_field_name, flat=True)
        super(ReversionSubqueryLookup, self).__init__(lhs, rhs)
        # Introspect the lhs and rhs, so we can fail early if it's unexpected.
        self.lhs_field = self.lhs.output_field
        self.rhs_field = self.rhs.model._meta.get_field(self.rhs_field_name)

    def _as_in_memory_sql(self, compiler, connection):
        """
        The most reliable strategy. The subquery is performed as two separate queries,
        buffering the subquery in application memory.

        This will work in all databases, but can use a lot of memory.
        """
        return compiler.compile(In(self.lhs, list(self.rhs.iterator())))

    def _as_in_database_sql(self, compiler, connection):
        """
        Theoretically the best strategy. The subquery is performed as a single database
        query, using nested SELECT.

        This will only work if the `Str` function supports the database.
        """
        lhs = self.lhs
        rhs = self.rhs
        # If fields are not the same internal type, we have to cast both to string.
        if self.lhs_field.get_internal_type() != self.rhs_field.get_internal_type():
            # If the left hand side is not a text field, we need to cast it.
            if not isinstance(self.lhs_field, (models.CharField, models.TextField)):
                lhs = Str(lhs)
            # If the right hand side is not a text field, we need to cast it.
            if not isinstance(self.rhs_field, (models.CharField, models.TextField)):
                rhs_str_name = "%s_str" % self.rhs_field.name
                rhs = rhs.annotate(**{
                    rhs_str_name: Str(self.rhs_field.name),
                }).values_list(rhs_str_name, flat=True)
        # All done!
        return compiler.compile(In(lhs, rhs))

    def as_sql(self, compiler, connection):
        """The fallback strategy for all databases is a safe in-memory subquery."""
        return self._as_in_memory_sql(compiler, connection)

    def as_sqlite(self, compiler, connection):
        """SQLite supports the `Str` function, so can use the efficient in-database subquery."""
        return self._as_in_database_sql(compiler, connection)

    def as_mysql(self, compiler, connection):
        """MySQL can choke on complex subqueries, so uses the safe in-memory subquery."""
        # TODO: Add a version selector to use the in-database subquery if a safe version is known.
        return self._as_in_memory_sql(compiler, connection)

    def as_postgresql(self, compiler, connection):
        """Postgres supports the `Str` function, so can use the efficient in-database subquery."""
        return self._as_in_database_sql(compiler, connection)
