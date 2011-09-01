"""Revision management for django-reversion."""


try:
    from functools import wraps
except ImportError:
    from django.utils.functional import wraps  # Python 2.4 fallback.

import operator
from threading import local
from weakref import WeakValueDictionary

from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.core.signals import request_finished
from django.db import models
from django.db.models import Q, Max
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, pre_delete

from reversion.errors import RevisionManagementError, RegistrationError
from reversion.models import Revision, Version, VERSION_ADD, VERSION_CHANGE, VERSION_DELETE, has_int_pk


class VersionAdapter(object):
    
    """Adapter class for serializing a registered model."""
    
    # Fields to include in the serialized data.
    fields = ()
    
    # Fields to exclude from the serialized data.
    exclude = ()
    
    # Foreign key relationships to follow when saving a version of this model.
    follow = ()
    
    # The serialization format to use.
    format = "json"
    
    def __init__(self, model):
        """Initializes the version adapter."""
        self.model = model
        
    def get_fields_to_serialize(self):
        """Returns an iterable of field names to serialize in the version data."""
        opts = self.model._meta
        fields = self.fields or (field.name for field in opts.local_fields + opts.local_many_to_many)
        fields = (opts.get_field(field) for field in fields if not field in self.exclude)
        for field in fields:
            if field.rel:
                yield field.name
            else:
                yield field.attname
    
    def get_followed_relations(self, obj):
        """Returns an iterable of related models that should be included in the revision data."""
        for relationship in self.follow:
            # Clear foreign key cache.
            try:
                related_field = obj._meta.get_field(relationship)
            except models.FieldDoesNotExist:
                pass
            else:
                if isinstance(related_field, models.ForeignKey):
                    if hasattr(obj, related_field.get_cache_name()):
                        delattr(obj, related_field.get_cache_name())
            # Get the referenced obj(s).
            try:
                related = getattr(obj, relationship, None)
            except ObjectDoesNotExist:
                continue
            if isinstance(related, models.Model):
                yield related
            elif isinstance(related, (models.Manager, QuerySet)):
                for related_obj in related.all():
                    yield related_obj
            elif related is not None:
                raise TypeError, "Cannot follow the relationship %r. Expected a model or QuerySet, found %r" % (relationship, related)
        # If a proxy model's parent is registered, add it.
        if obj._meta.proxy:
            parent_cls = obj._meta.parents.keys()[0]
            if self.is_registered(parent_cls):
                parent_obj = parent_cls.objects.get(pk=obj.pk)
                yield parent_obj
    
    def get_serialization_format(self):
        """Returns the serialization format to use."""
        return self.format
        
    def get_serialized_data(self, obj):
        """Returns a string of serialized data for the given obj."""
        return serializers.serialize(
            self.get_serialization_format(),
            (obj,),
            fields = self.get_fields_to_serialize(),
        )
        
    def get_version_data(self, obj, type_flag):
        """Creates the version data to be saved to the version model."""
        object_id = unicode(obj.pk)
        content_type = ContentType.objects.get_for_model(obj)
        if has_int_pk(obj.__class__):
            object_id_int = int(obj.pk)
        else:
            object_id_int = None
        return {
            "object_id": object_id,
            "object_id_int": object_id_int,
            "content_type": content_type,
            "format": self.get_serialization_format(),
            "serialized_data": self.get_serialized_data(obj),
            "object_repr": unicode(obj),
            "type": type_flag
        }

          
class RevisionContextManager(local):
    
    """Manages the state of the current revision."""
    
    def __init__(self):
        """Initializes the revision state."""
        self.clear()
        # Connect to the request finished signal.
        request_finished.connect(self._request_finished_receiver)
    
    def clear(self):
        """Puts the revision manager back into its default state."""
        self._objects = {}
        self._user = None
        self._comment = ""
        self._depth = 0
        self._is_invalid = False
        self._meta = []
        self._ignore_duplicates = False
    
    def is_active(self):
        """Returns whether there is an active revision for this thread."""
        return self._depth > 0
    
    def _assert_active(self):
        """Checks for an active revision, throwning an exception if none."""
        if not self.is_active():
            raise RevisionManagementError("There is no active revision for this thread")
        
    def start(self):
        """
        Begins a revision for this thread.
        
        This MUST be balanced by a call to `end`.  It is recommended that you
        leave these methods alone and instead use the revision context manager
        or the `create_on_success` decorator.
        """
        self._depth += 1
    
    def end(self):
        """Ends a revision for this thread."""
        self._assert_active()
        self._depth -= 1
        if self._depth == 0:
            try:
                if not self.is_invalid():
                    # Save the revision data.
                    for manager, manager_context in self._objects.iteritems():
                        manager.save_revision(
                            manager_context,
                            ignore_duplicates = self._ignore_duplicates,
                        )
            finally:
                self.clear()

    def invalidate(self):
        """Marks this revision as broken, so should not be commited."""
        self._assert_active()
        self._is_invalid = True
        
    def is_invalid(self):
        """Checks whether this revision is invalid."""
        return self._is_invalid
    
    def add_to_context(self, manager, obj, version_data):
        """Adds an object to the current revision."""
        self._assert_active()
        try:
            manager_context = self._objects[manager]
        except KeyError:
            manager_context = {}
            self._objects[manager] = manager_context
        manager_context[obj] = version_data

    def set_user(self, user):
        """Sets the current user for the revision."""
        self._assert_active()
        self._user = user
    
    def get_user(self):
        """Gets the current user for the revision."""
        self._assert_active()
        return self._user
        
    def set_comment(self, comment):
        """Sets the comments for the revision."""
        self._assert_active()
        self._comment = comment
    
    def get_comment(self, comment):
        """Gets the current comment for the revision."""
        self._assert_active()
        return self_comment
        
    def add_meta(self, cls, **kwargs):
        """Adds a class of meta information to the current revision."""
        self._assert_active()
        self._meta.append((cls, kwargs))
    
    def set_ignore_duplicates(self, ignore_duplicates):
        """Sets whether to ignore duplicate revisions."""
        self._assert_active()
        self._ignore_duplicates = ignore_duplicates
        
    def get_ignore_duplicates(self, ignore_duplicates):
        """Gets whether to ignore duplicate revisions."""
        self._assert_active()
        return self._ignore_duplicates
    
    # Signal receivers.
    
    def _request_finished_receiver(self, **kwargs):
        """
        Called at the end of a request, ensuring that any open revisions
        are closed. Not closing all active revisions can cause memory leaks
        and weird behaviour.
        
        If you use the low level API correctly, this shouldn't ever be the case.
        If it does happen, a RevisionManagementError will be raised.
        """
        if self.is_active():
            raise RevisionManagementError(
                "Request finished with an open revision. All calls to revision.start() "
                "should be balanced by a call to revision.end()."
            )
    
    # High-level context management.
    
    def __enter__(self):
        """Enters a block of revision management."""
        self.start()
        
    def __exit__(self, exc_type, exc_value, traceback):
        """Leaves a block of revision management."""
        try:
            if exc_type is not None:
                self.invalidate()
        finally:
            self.end()
        return False
        
    def context(self):
        """Defines a revision management context."""
        return self  # TODO: Replace with contextlib context manager when Django drops 2.4 compatibility.
        
    def create_revision(self, func):
        """Creates a revision when the given function exits successfully."""
        def _create_on_success(*args, **kwargs):
            self.start()
            try:
                try:
                    result = func(*args, **kwargs)
                except:
                    self.invalidate()
                    raise
            finally:
                self.end()
            return result
        return wraps(func)(_create_on_success)


# A shared, thread-safe context manager.
revision_context_manager = RevisionContextManager()
   
   
class RevisionManager(object):
    
    """Manages the configuration and creation of revisions."""
    
    _created_managers = WeakValueDictionary()
    
    @classmethod
    def get_created_managers(cls):
        """Returns all created revision managers."""
        return list(cls._created_managers.items())
    
    def __init__(self, manager_slug, revision_context_manager=revision_context_manager):
        """Initializes the revision manager."""
         # Check the slug is unique for this revision manager.
        if manager_slug in RevisionManager._created_managers:
            raise RevisionManagementError("A revision manager has already been created with the slug %r" % manager_slug)
        # Store a reference to this manager.
        self.__class__._created_managers[manager_slug] = self
        # Store config params.
        self._manager_slug = manager_slug
        self._registered_models = {}
        self._revision_context_manager = revision_context_manager
        # Proxies to common context methods.
        self.create_on_success = revision_context_manager.create_revision
        self.add_meta = revision_context_manager.add_meta

    # Registration methods.

    def is_registered(self, model):
        """
        Checks whether the given model has been registered with this revision
        manager.
        """
        return model in self._registered_models
        
    def register(self, model, adapter_cls=VersionAdapter, **field_overrides):
        """Registers a model with this revision manager."""
        # Prevent multiple registration.
        if self.is_registered(model):
            raise RegistrationError, "%r has already been registered with django-reversion" % model
        # Ensure the parent model of proxy models is registered.
        if model._meta.proxy and not self.is_registered(model._meta.parents.keys()[0]):
            raise RegistrationError, "%r is a proxy model, and its parent has not been registered with django-reversion." % model
        # Perform any customization.
        if field_overrides:
            adapter_cls = type("Custom" + adapter_cls.__name__, (adapter_cls,), field_overrides)
        # Perform the registration.
        adapter_obj = adapter_cls(model)
        self._registered_models[model] = adapter_obj
        # Connect to the post save signal of the model.
        post_save.connect(self._post_save_receiver, model)
        pre_delete.connect(self._pre_delete_receiver, model)
    
    def get_adapter(self, model):
        """Returns the registration information for the given model class."""
        if self.is_registered(model):
            return self._registered_models[model]
        else:
            raise RegistrationError, "%r has not been registered with django-reversion" % model
        
    def unregister(self, model):
        """Removes a model from version control."""
        if not self.is_registered(model):
            raise RegistrationError, "%r has not been registered with django-reversion" % model
        del self._registered_models[model]
        post_save.disconnect(self._post_save_receiver, model)
        pre_delete.disconnect(self._pre_delete_receiver, model)
        
    def _follow_relationships(self, object_dict):
        """
        Follows all the registered relationships in the given set of models to
        yield a set containing the original models plus all their related
        models.
        """
        result_dict = {}
        def _follow_relationships(obj):
            # Prevent recursion.
            if obj in result_dict or obj.pk is None:  # This last condition is because during a delete action the parent field for a subclassing model will be set to None.
                return
            adapter = self.get_adapter(obj.__class__)
            result_dict[obj] = adapter.get_version_data(obj, VERSION_CHANGE)
            # Follow relations.
            for related in adapter.get_followed_relations(obj):
                _follow_relationships(related)
        map(_follow_relationships, object_dict)
        # Place in the original reversions models explicitly added to the revision.
        result_dict.update(object_dict)
        return result_dict
        
    def save_revision(self, objects, ignore_duplicates=False, user=None, comment="", meta=()):
        """Saves a new revision."""
        # Adapt the objects to a dict.
        if isinstance(objects, (list, tuple)):
            objects = dict(
                (obj, self.get_adapter(obj.__class__).get_version_data(obj, VERSION_CHANGE))
                for obj in objects
            )
        # Create the revision.
        if objects:
            # Follow relationships.
            revision_set = self._follow_relationships(objects)
            # Create all the versions without saving them
            new_versions = []
            for obj, version_data in revision_set.iteritems():
                # Proxy models should not actually be saved to the revision set.
                if obj._meta.proxy:
                    continue
                new_versions.append(Version(**version_data))
            # Check if there's some change in all the revision's objects.
            save_revision = True
            if ignore_duplicates:
                # Find the latest revision amongst the latest previous version of each object.
                subqueries = [Q(object_id=version.object_id, content_type=version.content_type) for version in new_versions]
                subqueries = reduce(operator.or_, subqueries)
                latest_revision = Version.objects.filter(subqueries).aggregate(Max("revision"))["revision__max"]
                # If we have a latest revision, compare it to the current revision.
                if latest_revision is not None:
                    previous_versions = Version.objects.filter(revision=latest_revision).values_list("serialized_data", flat=True)
                    if len(previous_versions) == len(new_versions):
                        all_serialized_data = [version.serialized_data for version in new_versions]
                        if sorted(previous_versions) == sorted(all_serialized_data):
                            save_revision = False
            # Only save if we're always saving, or have changes.
            if save_revision:
                # Save a new revision.
                revision = Revision.objects.create(
                    manager_slug = self._manager_slug,
                    user = user,
                    comment = comment,
                )
                # Save version models.
                for version in new_versions:
                    version.revision = revision
                    version.save()
                # Save the meta information.
                for cls, kwargs in meta:
                    cls._default_manager.create(revision=revision, **kwargs)
    
    # Context management.
    
    def __enter__(self, *args, **kwargs):
        """Enters a revision management block."""
        return self._revision_context_manager.__enter__(*args, **kwargs)
        
    def __exit__(self, *args, **kwargs):
        """Exists a revision management block."""
        return self._revision_context_manager.__exit__(*args, **kwargs)
    
    # Revision meta data.
    
    user = property(
        lambda self: self._revision_context_manager.get_user(),
        lambda self, user: self._revision_context_manager.set_user(user),
    )
    
    comment = property(
        lambda self: self._revision_context_manager.get_comment(),
        lambda self, comment: self._revision_context_manager.set_comment(comment),
    )
    
    ignore_duplicates = property(
        lambda self: self._revision_context_manager.get_ignore_duplicates(),
        lambda self, ignore_duplicates: self._revision_context_manager.set_ignore_duplicates(ignore_duplicates)
    )
        
    # Signal receivers.
        
    def _post_save_receiver(self, instance, created, **kwargs):
        """Adds registered models to the current revision, if any."""
        if self._revision_context_manager.is_active():
            adapter = self.get_adapter(instance.__class__)
            if created:
                version_data = adapter.get_version_data(instance, VERSION_ADD)
            else:
                version_data = adapter.get_version_data(instance, VERSION_CHANGE)
            self._revision_context_manager.add_to_context(self, instance, version_data)
            
    def _pre_delete_receiver(self, instance, **kwargs):
        """Adds registerted models to the current revision, if any."""
        if self._revision_context_manager.is_active():
            adapter = self.get_adapter(instance.__class__)
            version_data = adapter.get_version_data(instance, VERSION_DELETE)
            self._revision_context_manager.add_to_context(self, instance, version_data)

        
# A shared revision manager.
revision = RevisionManager("default")