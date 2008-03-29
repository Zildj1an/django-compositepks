from django.db import connection, transaction
from django.db.models import signals, get_model
from django.db.models.fields import AutoField, Field, IntegerField, PositiveIntegerField, PositiveSmallIntegerField, get_ul_class
from django.db.models.related import RelatedObject
from django.utils.text import capfirst
from django.utils.translation import ugettext_lazy, string_concat, ungettext, ugettext as _
from django.utils.functional import curry
from django.utils.encoding import smart_unicode
from django.core import validators
from django import oldforms
from django import newforms as forms
from django.dispatch import dispatcher

try:
    set
except NameError:
    from sets import Set as set   # Python 2.3 fallback

# Values for Relation.edit_inline.
TABULAR, STACKED = 1, 2

RECURSIVE_RELATIONSHIP_CONSTANT = 'self'

pending_lookups = {}

def add_lazy_relation(cls, field, relation):
    """
    Adds a lookup on ``cls`` when a related field is defined using a string,
    i.e.::
    
        class MyModel(Model):
            fk = ForeignKey("AnotherModel")
            
    This string can be:
    
        * RECURSIVE_RELATIONSHIP_CONSTANT (i.e. "self") to indicate a recursive
          relation.
          
        * The name of a model (i.e "AnotherModel") to indicate another model in
          the same app.
          
        * An app-label and model name (i.e. "someapp.AnotherModel") to indicate
          another model in a different app.
          
    If the other model hasn't yet been loaded -- almost a given if you're using
    lazy relationships -- then the relation won't be set up until the
    class_prepared signal fires at the end of model initialization.
    """
    # Check for recursive relations
    if relation == RECURSIVE_RELATIONSHIP_CONSTANT:
        app_label = cls._meta.app_label
        model_name = cls.__name__
    
    else:
        # Look for an "app.Model" relation
        try:
            app_label, model_name = relation.split(".")
        except ValueError:
            # If we can't split, assume a model in current app
            app_label = cls._meta.app_label
            model_name = relation
    
    # Try to look up the related model, and if it's already loaded resolve the
    # string right away. If get_model returns None, it means that the related
    # model isn't loaded yet, so we need to pend the relation until the class 
    # is prepared.
    model = get_model(app_label, model_name, False)
    if model:
        field.rel.to = model
        field.do_related_class(model, cls)
    else:
        key = (app_label, model_name)
        value = (cls, field)
        pending_lookups.setdefault(key, []).append(value)
    
def do_pending_lookups(sender):
    """
    Handle any pending relations to the sending model. Sent from class_prepared.
    """
    key = (sender._meta.app_label, sender.__name__)
    for cls, field in pending_lookups.pop(key, []):
        field.rel.to = sender
        field.do_related_class(sender, cls)

dispatcher.connect(do_pending_lookups, signal=signals.class_prepared)

def manipulator_valid_rel_key(f, self, field_data, all_data):
    "Validates that the value is a valid foreign key"
    klass = f.rel.to
    try:
        klass._default_manager.get(**{f.rel.field_name: field_data})
    except klass.DoesNotExist:
        raise validators.ValidationError, _("Please enter a valid %s.") % f.verbose_name

#HACK
class RelatedField(object):
    def contribute_to_class(self, cls, name):
        sup = super(RelatedField, self)

        # Add an accessor to allow easy determination of the related query path for this field
        self.related_query_name = curry(self._get_related_query_name, cls._meta)

        if hasattr(sup, 'contribute_to_class'):
            sup.contribute_to_class(cls, name)
        other = self.rel.to
        if isinstance(other, basestring):
            add_lazy_relation(cls, self, other)
        else:
            self.do_related_class(other, cls)

    def set_attributes_from_rel(self):
        self.name = self.name or (self.rel.to._meta.object_name.lower() + '_' + self.rel.to._meta.pk.name)
        self.verbose_name = self.verbose_name or self.rel.to._meta.verbose_name
        self.rel.field_name = self.rel.field_name or self.rel.to._meta.pk.name

    def do_related_class(self, other, cls):
        self.set_attributes_from_rel()
        related = RelatedObject(other, cls, self)
        self.contribute_to_related_class(other, related)

    def get_db_prep_lookup(self, lookup_type, value):
        # If we are doing a lookup on a Related Field, we must be
        # comparing object instances. The value should be the PK of value,
        # not value itself.
        def pk_trace(value):
            # Value may be a primary key, or an object held in a relation.
            # If it is an object, then we need to get the primary key value for
            # that object. In certain conditions (especially one-to-one relations),
            # the primary key may itself be an object - so we need to keep drilling
            # down until we hit a value that can be used for a comparison.
            v = value
            try:
                while True:
                    v = getattr(v, v._meta.pk.name)
            except AttributeError:
                pass
            return v

        if lookup_type == 'exact':
            return [pk_trace(value)]
        if lookup_type == 'in':
            return [pk_trace(v) for v in value]
        elif lookup_type == 'isnull':
            return []
        raise TypeError, "Related Field has invalid lookup: %s" % lookup_type

    def _get_related_query_name(self, opts):
        # This method defines the name that can be used to identify this related object
        # in a table-spanning query. It uses the lower-cased object_name by default,
        # but this can be overridden with the "related_name" option.
        return self.rel.related_name or opts.object_name.lower()

class SingleRelatedObjectDescriptor(object):
    # This class provides the functionality that makes the related-object
    # managers available as attributes on a model class, for fields that have
    # a single "remote" value, on the class pointed to by a related field.
    # In the example "place.restaurant", the restaurant attribute is a
    # SingleRelatedObjectDescriptor instance.
    def __init__(self, related):
        self.related = related

    def __get__(self, instance, instance_type=None):
        if instance is None:
            raise AttributeError, "%s must be accessed via instance" % self.related.opts.object_name

        params = {'%s__pk' % self.related.field.name: instance._get_pk_val()}
        rel_obj = self.related.model._default_manager.get(**params)
        return rel_obj

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "%s must be accessed via instance" % self.related.opts.object_name
        # Set the value of the related field
        setattr(value, self.related.field.rel.get_related_field().attname, instance)

        # Clear the cache, if it exists
        try:
            delattr(value, self.related.field.get_cache_name())
        except AttributeError:
            pass

class ReverseSingleRelatedObjectDescriptor(object):
    # This class provides the functionality that makes the related-object
    # managers available as attributes on a model class, for fields that have
    # a single "remote" value, on the class that defines the related field.
    # In the example "choice.poll", the poll attribute is a
    # ReverseSingleRelatedObjectDescriptor instance.
    def __init__(self, field_with_rel):
        self.field = field_with_rel

    def __get__(self, instance, instance_type=None):
        if instance is None:
            raise AttributeError, "%s must be accessed via instance" % self.field.name
        cache_name = self.field.get_cache_name()
        try:
            return getattr(instance, cache_name)
        except AttributeError:
            val = getattr(instance, self.field.attname)
            if val is None:
                # If NULL is an allowed value, return it.
                if self.field.null:
                    return None
                raise self.field.rel.to.DoesNotExist
            other_field = self.field.rel.get_related_field()
            if other_field.rel:
                params = {'%s__pk' % self.field.rel.field_name: val}
            else:
                params = {'%s__exact' % self.field.rel.field_name: val}
            rel_obj = self.field.rel.to._default_manager.get(**params)
            setattr(instance, cache_name, rel_obj)
            return rel_obj

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "%s must be accessed via instance" % self._field.name
        # Set the value of the related field
        try:
            val = getattr(value, self.field.rel.get_related_field().attname)
        except AttributeError:
            val = None
        setattr(instance, self.field.attname, val)

        # Clear the cache, if it exists
        try:
            delattr(instance, self.field.get_cache_name())
        except AttributeError:
            pass

class ForeignRelatedObjectsDescriptor(object):
    # This class provides the functionality that makes the related-object
    # managers available as attributes on a model class, for fields that have
    # multiple "remote" values and have a ForeignKey pointed at them by
    # some other model. In the example "poll.choice_set", the choice_set
    # attribute is a ForeignRelatedObjectsDescriptor instance.
    def __init__(self, related):
        self.related = related   # RelatedObject instance

    def __get__(self, instance, instance_type=None):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        rel_field = self.related.field
        rel_model = self.related.model

        # Dynamically create a class that subclasses the related
        # model's default manager.
        superclass = self.related.model._default_manager.__class__

        class RelatedManager(superclass):
            def get_query_set(self):
                return superclass.get_query_set(self).filter(**(self.core_filters))

            def add(self, *objs):
                for obj in objs:
                    setattr(obj, rel_field.name, instance)
                    obj.save()
            add.alters_data = True

            def create(self, **kwargs):
                new_obj = self.model(**kwargs)
                self.add(new_obj)
                return new_obj
            create.alters_data = True

            # remove() and clear() are only provided if the ForeignKey can have a value of null.
            if rel_field.null:
                def remove(self, *objs):
                    val = getattr(instance, rel_field.rel.get_related_field().attname)
                    for obj in objs:
                        # Is obj actually part of this descriptor set?
                        if getattr(obj, rel_field.attname) == val:
                            setattr(obj, rel_field.name, None)
                            obj.save()
                        else:
                            raise rel_field.rel.to.DoesNotExist, "%r is not related to %r." % (obj, instance)
                remove.alters_data = True

                def clear(self):
                    for obj in self.all():
                        setattr(obj, rel_field.name, None)
                        obj.save()
                clear.alters_data = True

        manager = RelatedManager()
        manager.core_filters = {'%s__pk' % rel_field.name: getattr(instance, rel_field.rel.get_related_field().attname)}
        manager.model = self.related.model

        return manager

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        manager = self.__get__(instance)
        # If the foreign key can support nulls, then completely clear the related set.
        # Otherwise, just move the named objects into the set.
        if self.related.field.null:
            manager.clear()
        manager.add(*value)

def create_many_related_manager(superclass):
    """Creates a manager that subclasses 'superclass' (which is a Manager)
    and adds behavior for many-to-many related objects."""
    class ManyRelatedManager(superclass):
        def __init__(self, model=None, core_filters=None, instance=None, symmetrical=None,
                join_table=None, source_col_name=None, target_col_name=None):
            super(ManyRelatedManager, self).__init__()
            self.core_filters = core_filters
            self.model = model
            self.symmetrical = symmetrical
            self.instance = instance
            self.join_table = join_table
            self.source_col_name = source_col_name
            self.target_col_name = target_col_name
            self._pk_val = self.instance._get_pk_val()
            if self._pk_val is None:
                raise ValueError("%r instance needs to have a primary key value before a many-to-many relationship can be used." % model)

        def get_query_set(self):
            return superclass.get_query_set(self).filter(**(self.core_filters))

        def add(self, *objs):
            self._add_items(self.source_col_name, self.target_col_name, *objs)

            # If this is a symmetrical m2m relation to self, add the mirror entry in the m2m table
            if self.symmetrical:
                self._add_items(self.target_col_name, self.source_col_name, *objs)
        add.alters_data = True

        def remove(self, *objs):
            self._remove_items(self.source_col_name, self.target_col_name, *objs)

            # If this is a symmetrical m2m relation to self, remove the mirror entry in the m2m table
            if self.symmetrical:
                self._remove_items(self.target_col_name, self.source_col_name, *objs)
        remove.alters_data = True

        def clear(self):
            self._clear_items(self.source_col_name)

            # If this is a symmetrical m2m relation to self, clear the mirror entry in the m2m table
            if self.symmetrical:
                self._clear_items(self.target_col_name)
        clear.alters_data = True

        def create(self, **kwargs):
            new_obj = self.model(**kwargs)
            new_obj.save()
            self.add(new_obj)
            return new_obj
        create.alters_data = True

        def _add_items(self, source_col_name, target_col_name, *objs):
            # join_table: name of the m2m link table
            # source_col_name: the PK colname in join_table for the source object
            # target_col_name: the PK colname in join_table for the target object
            # *objs - objects to add. Either object instances, or primary keys of object instances.

            # If there aren't any objects, there is nothing to do.
            if objs:
                # Check that all the objects are of the right type
                new_ids = set()
                for obj in objs:
                    if isinstance(obj, self.model):
                        new_ids.add(obj._get_pk_val())
                    else:
                        new_ids.add(obj)
                # Add the newly created or already existing objects to the join table.
                # First find out which items are already added, to avoid adding them twice
                cursor = connection.cursor()
                cursor.execute("SELECT %s FROM %s WHERE %s = %%s AND %s IN (%s)" % \
                    (target_col_name, self.join_table, source_col_name,
                    target_col_name, ",".join(['%s'] * len(new_ids))),
                    [self._pk_val] + list(new_ids))
                existing_ids = set([row[0] for row in cursor.fetchall()])

                # Add the ones that aren't there already
                for obj_id in (new_ids - existing_ids):
                    cursor.execute("INSERT INTO %s (%s, %s) VALUES (%%s, %%s)" % \
                        (self.join_table, source_col_name, target_col_name),
                        [self._pk_val, obj_id])
                transaction.commit_unless_managed()

        def _remove_items(self, source_col_name, target_col_name, *objs):
            # source_col_name: the PK colname in join_table for the source object
            # target_col_name: the PK colname in join_table for the target object
            # *objs - objects to remove

            # If there aren't any objects, there is nothing to do.
            if objs:
                # Check that all the objects are of the right type
                old_ids = set()
                for obj in objs:
                    if isinstance(obj, self.model):
                        old_ids.add(obj._get_pk_val())
                    else:
                        old_ids.add(obj)
                # Remove the specified objects from the join table
                cursor = connection.cursor()
                cursor.execute("DELETE FROM %s WHERE %s = %%s AND %s IN (%s)" % \
                    (self.join_table, source_col_name,
                    target_col_name, ",".join(['%s'] * len(old_ids))),
                    [self._pk_val] + list(old_ids))
                transaction.commit_unless_managed()

        def _clear_items(self, source_col_name):
            # source_col_name: the PK colname in join_table for the source object
            cursor = connection.cursor()
            cursor.execute("DELETE FROM %s WHERE %s = %%s" % \
                (self.join_table, source_col_name),
                [self._pk_val])
            transaction.commit_unless_managed()

    return ManyRelatedManager

class ManyRelatedObjectsDescriptor(object):
    # This class provides the functionality that makes the related-object
    # managers available as attributes on a model class, for fields that have
    # multiple "remote" values and have a ManyToManyField pointed at them by
    # some other model (rather than having a ManyToManyField themselves).
    # In the example "publication.article_set", the article_set attribute is a
    # ManyRelatedObjectsDescriptor instance.
    def __init__(self, related):
        self.related = related   # RelatedObject instance

    def __get__(self, instance, instance_type=None):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        # Dynamically create a class that subclasses the related
        # model's default manager.
        rel_model = self.related.model
        superclass = rel_model._default_manager.__class__
        RelatedManager = create_many_related_manager(superclass)

        qn = connection.ops.quote_name
        manager = RelatedManager(
            model=rel_model,
            core_filters={'%s__pk' % self.related.field.name: instance._get_pk_val()},
            instance=instance,
            symmetrical=False,
            join_table=qn(self.related.field.m2m_db_table()),
            source_col_name=qn(self.related.field.m2m_reverse_name()),
            target_col_name=qn(self.related.field.m2m_column_name())
        )

        return manager

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)

class ReverseManyRelatedObjectsDescriptor(object):
    # This class provides the functionality that makes the related-object
    # managers available as attributes on a model class, for fields that have
    # multiple "remote" values and have a ManyToManyField defined in their
    # model (rather than having another model pointed *at* them).
    # In the example "article.publications", the publications attribute is a
    # ReverseManyRelatedObjectsDescriptor instance.
    def __init__(self, m2m_field):
        self.field = m2m_field

    def __get__(self, instance, instance_type=None):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        # Dynamically create a class that subclasses the related
        # model's default manager.
        rel_model=self.field.rel.to
        superclass = rel_model._default_manager.__class__
        RelatedManager = create_many_related_manager(superclass)

        qn = connection.ops.quote_name
        manager = RelatedManager(
            model=rel_model,
            core_filters={'%s__pk' % self.field.related_query_name(): instance._get_pk_val()},
            instance=instance,
            symmetrical=(self.field.rel.symmetrical and instance.__class__ == rel_model),
            join_table=qn(self.field.m2m_db_table()),
            source_col_name=qn(self.field.m2m_column_name()),
            target_col_name=qn(self.field.m2m_reverse_name())
        )

        return manager

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)

class ForeignKey(RelatedField, Field):
    empty_strings_allowed = False
    def __init__(self, to, to_field=None, **kwargs):
        try:
            to_name = to._meta.object_name.lower()
        except AttributeError: # to._meta doesn't exist, so it must be RECURSIVE_RELATIONSHIP_CONSTANT
            assert isinstance(to, basestring), "ForeignKey(%r) is invalid. First parameter to ForeignKey must be either a model, a model name, or the string %r" % (to, RECURSIVE_RELATIONSHIP_CONSTANT)
        else:
            to_field = to_field or to._meta.pk.name
        kwargs['verbose_name'] = kwargs.get('verbose_name', '')

        if 'edit_inline_type' in kwargs:
            import warnings
            warnings.warn("edit_inline_type is deprecated. Use edit_inline instead.", DeprecationWarning)
            kwargs['edit_inline'] = kwargs.pop('edit_inline_type')

        kwargs['rel'] = ManyToOneRel(to, to_field,
            num_in_admin=kwargs.pop('num_in_admin', 3),
            min_num_in_admin=kwargs.pop('min_num_in_admin', None),
            max_num_in_admin=kwargs.pop('max_num_in_admin', None),
            num_extra_on_change=kwargs.pop('num_extra_on_change', 1),
            edit_inline=kwargs.pop('edit_inline', False),
            related_name=kwargs.pop('related_name', None),
            limit_choices_to=kwargs.pop('limit_choices_to', None),
            lookup_overrides=kwargs.pop('lookup_overrides', None))
        Field.__init__(self, **kwargs)

        self.db_index = True

    def get_attname(self):
        return '%s_id' % self.name

    def get_validator_unique_lookup_type(self):
        return '%s__%s__exact' % (self.name, self.rel.get_related_field().name)

    def prepare_field_objs_and_params(self, manipulator, name_prefix):
        params = {'validator_list': self.validator_list[:], 'member_name': name_prefix + self.attname}
        if self.radio_admin:
            field_objs = [oldforms.RadioSelectField]
            params['ul_class'] = get_ul_class(self.radio_admin)
        else:
            if self.null:
                field_objs = [oldforms.NullSelectField]
            else:
                field_objs = [oldforms.SelectField]
        params['choices'] = self.get_choices_default()
        return field_objs, params

    def get_default(self):
        "Here we check if the default value is an object and return the to_field if so."
        field_default = super(ForeignKey, self).get_default()
        if isinstance(field_default, self.rel.to):
            return getattr(field_default, self.rel.get_related_field().attname)
        return field_default

    def get_manipulator_field_objs(self):
        rel_field = self.rel.get_related_field()
        return [oldforms.IntegerField]

    def get_db_prep_save(self, value):
        if value == '' or value == None:
            return None
        else:
            return self.rel.get_related_field().get_db_prep_save(value)

    def flatten_data(self, follow, obj=None):
        if not obj:
            # In required many-to-one fields with only one available choice,
            # select that one available choice. Note: For SelectFields
            # (radio_admin=False), we have to check that the length of choices
            # is *2*, not 1, because SelectFields always have an initial
            # "blank" value. Otherwise (radio_admin=True), we check that the
            # length is 1.
            if not self.blank and self.choices:
                choice_list = self.get_choices_default()
                if self.radio_admin and len(choice_list) == 1:
                    return {self.attname: choice_list[0][0]}
                if not self.radio_admin and len(choice_list) == 2:
                    return {self.attname: choice_list[1][0]}
        return Field.flatten_data(self, follow, obj)

    def contribute_to_class(self, cls, name):
        super(ForeignKey, self).contribute_to_class(cls, name)
        setattr(cls, self.name, ReverseSingleRelatedObjectDescriptor(self))

    def contribute_to_related_class(self, cls, related):
        setattr(cls, related.get_accessor_name(), ForeignRelatedObjectsDescriptor(related))

    def formfield(self, **kwargs):
        defaults = {'form_class': forms.ModelChoiceField, 'queryset': self.rel.to._default_manager.all()}
        defaults.update(kwargs)
        return super(ForeignKey, self).formfield(**defaults)

    def db_type(self):
        # The database column type of a ForeignKey is the column type
        # of the field to which it points. An exception is if the ForeignKey
        # points to an AutoField/PositiveIntegerField/PositiveSmallIntegerField,
        # in which case the column type is simply that of an IntegerField.
        rel_field = self.rel.get_related_field()
        if isinstance(rel_field, (AutoField, PositiveIntegerField, PositiveSmallIntegerField)):
            return IntegerField().db_type()
        return rel_field.db_type()

class OneToOneField(RelatedField, IntegerField):
    def __init__(self, to, to_field=None, **kwargs):
        try:
            to_name = to._meta.object_name.lower()
        except AttributeError: # to._meta doesn't exist, so it must be RECURSIVE_RELATIONSHIP_CONSTANT
            assert isinstance(to, basestring), "OneToOneField(%r) is invalid. First parameter to OneToOneField must be either a model, a model name, or the string %r" % (to, RECURSIVE_RELATIONSHIP_CONSTANT)
        else:
            to_field = to_field or to._meta.pk.name
        kwargs['verbose_name'] = kwargs.get('verbose_name', '')

        if 'edit_inline_type' in kwargs:
            import warnings
            warnings.warn("edit_inline_type is deprecated. Use edit_inline instead.", DeprecationWarning)
            kwargs['edit_inline'] = kwargs.pop('edit_inline_type')

        kwargs['rel'] = OneToOneRel(to, to_field,
            num_in_admin=kwargs.pop('num_in_admin', 0),
            edit_inline=kwargs.pop('edit_inline', False),
            related_name=kwargs.pop('related_name', None),
            limit_choices_to=kwargs.pop('limit_choices_to', None),
            lookup_overrides=kwargs.pop('lookup_overrides', None))
        kwargs['primary_key'] = True
        IntegerField.__init__(self, **kwargs)

        self.db_index = True

    def get_attname(self):
        return '%s_id' % self.name

    def get_validator_unique_lookup_type(self):
        return '%s__%s__exact' % (self.name, self.rel.get_related_field().name)

    # TODO: Copied from ForeignKey... putting this in RelatedField adversely affects
    # ManyToManyField. This works for now.
    def prepare_field_objs_and_params(self, manipulator, name_prefix):
        params = {'validator_list': self.validator_list[:], 'member_name': name_prefix + self.attname}
        if self.radio_admin:
            field_objs = [oldforms.RadioSelectField]
            params['ul_class'] = get_ul_class(self.radio_admin)
        else:
            if self.null:
                field_objs = [oldforms.NullSelectField]
            else:
                field_objs = [oldforms.SelectField]
        params['choices'] = self.get_choices_default()
        return field_objs, params

    def contribute_to_class(self, cls, name):
        super(OneToOneField, self).contribute_to_class(cls, name)
        setattr(cls, self.name, ReverseSingleRelatedObjectDescriptor(self))

    def contribute_to_related_class(self, cls, related):
        setattr(cls, related.get_accessor_name(), SingleRelatedObjectDescriptor(related))
        if not cls._meta.one_to_one_field:
            cls._meta.one_to_one_field = self

    def formfield(self, **kwargs):
        defaults = {'form_class': forms.ModelChoiceField, 'queryset': self.rel.to._default_manager.all()}
        defaults.update(kwargs)
        return super(OneToOneField, self).formfield(**defaults)

    def db_type(self):
        # The database column type of a OneToOneField is the column type
        # of the field to which it points. An exception is if the OneToOneField
        # points to an AutoField/PositiveIntegerField/PositiveSmallIntegerField,
        # in which case the column type is simply that of an IntegerField.
        rel_field = self.rel.get_related_field()
        if isinstance(rel_field, (AutoField, PositiveIntegerField, PositiveSmallIntegerField)):
            return IntegerField().db_type()
        return rel_field.db_type()

class ManyToManyField(RelatedField, Field):
    def __init__(self, to, **kwargs):
        kwargs['verbose_name'] = kwargs.get('verbose_name', None)
        kwargs['rel'] = ManyToManyRel(to,
            num_in_admin=kwargs.pop('num_in_admin', 0),
            related_name=kwargs.pop('related_name', None),
            limit_choices_to=kwargs.pop('limit_choices_to', None),
            symmetrical=kwargs.pop('symmetrical', True))
        self.db_table = kwargs.pop('db_table', None)
        Field.__init__(self, **kwargs)

        msg = ugettext_lazy('Hold down "Control", or "Command" on a Mac, to select more than one.')
        self.help_text = string_concat(self.help_text, ' ', msg)

    def get_manipulator_field_objs(self):
        choices = self.get_choices_default()
        return [curry(oldforms.SelectMultipleField, size=min(max(len(choices), 5), 15), choices=choices)]

    def get_choices_default(self):
        return Field.get_choices(self, include_blank=False)

    def _get_m2m_db_table(self, opts):
        "Function that can be curried to provide the m2m table name for this relation"
        if self.db_table:
            return self.db_table
        else:
            return '%s_%s' % (opts.db_table, self.name)

    def _get_m2m_column_name(self, related):
        "Function that can be curried to provide the source column name for the m2m table"
        # If this is an m2m relation to self, avoid the inevitable name clash
        if related.model == related.parent_model:
            return 'from_' + related.model._meta.object_name.lower() + '_id'
        else:
            return related.model._meta.object_name.lower() + '_id'

    def _get_m2m_reverse_name(self, related):
        "Function that can be curried to provide the related column name for the m2m table"
        # If this is an m2m relation to self, avoid the inevitable name clash
        if related.model == related.parent_model:
            return 'to_' + related.parent_model._meta.object_name.lower() + '_id'
        else:
            return related.parent_model._meta.object_name.lower() + '_id'

    def isValidIDList(self, field_data, all_data):
        "Validates that the value is a valid list of foreign keys"
        mod = self.rel.to
        try:
            pks = map(int, field_data.split(','))
        except ValueError:
            # the CommaSeparatedIntegerField validator will catch this error
            return
        objects = mod._default_manager.in_bulk(pks)
        if len(objects) != len(pks):
            badkeys = [k for k in pks if k not in objects]
            raise validators.ValidationError, ungettext("Please enter valid %(self)s IDs. The value %(value)r is invalid.",
                    "Please enter valid %(self)s IDs. The values %(value)r are invalid.", len(badkeys)) % {
                'self': self.verbose_name,
                'value': len(badkeys) == 1 and badkeys[0] or tuple(badkeys),
            }

    def flatten_data(self, follow, obj = None):
        new_data = {}
        if obj:
            instance_ids = [instance._get_pk_val() for instance in getattr(obj, self.name).all()]
            new_data[self.name] = instance_ids
        else:
            # In required many-to-many fields with only one available choice,
            # select that one available choice.
            if not self.blank and not self.rel.edit_inline:
                choices_list = self.get_choices_default()
                if len(choices_list) == 1:
                    new_data[self.name] = [choices_list[0][0]]
        return new_data

    def contribute_to_class(self, cls, name):
        super(ManyToManyField, self).contribute_to_class(cls, name)
        # Add the descriptor for the m2m relation
        setattr(cls, self.name, ReverseManyRelatedObjectsDescriptor(self))

        # Set up the accessor for the m2m table name for the relation
        self.m2m_db_table = curry(self._get_m2m_db_table, cls._meta)

    def contribute_to_related_class(self, cls, related):
        # m2m relations to self do not have a ManyRelatedObjectsDescriptor,
        # as it would be redundant - unless the field is non-symmetrical.
        if related.model != related.parent_model or not self.rel.symmetrical:
            # Add the descriptor for the m2m relation
            setattr(cls, related.get_accessor_name(), ManyRelatedObjectsDescriptor(related))

        # Set up the accessors for the column names on the m2m table
        self.m2m_column_name = curry(self._get_m2m_column_name, related)
        self.m2m_reverse_name = curry(self._get_m2m_reverse_name, related)

    def set_attributes_from_rel(self):
        pass

    def value_from_object(self, obj):
        "Returns the value of this field in the given model instance."
        return getattr(obj, self.attname).all()

    def save_form_data(self, instance, data):
        setattr(instance, self.attname, data)
        
    def formfield(self, **kwargs):
        defaults = {'form_class': forms.ModelMultipleChoiceField, 'queryset': self.rel.to._default_manager.all()}
        defaults.update(kwargs)
        # If initial is passed in, it's a list of related objects, but the
        # MultipleChoiceField takes a list of IDs.
        if defaults.get('initial') is not None:
            defaults['initial'] = [i._get_pk_val() for i in defaults['initial']]
        return super(ManyToManyField, self).formfield(**defaults)

    def db_type(self):
        # A ManyToManyField is not represented by a single column,
        # so return None.
        return None

class ManyToOneRel(object):
    def __init__(self, to, field_name, num_in_admin=3, min_num_in_admin=None,
        max_num_in_admin=None, num_extra_on_change=1, edit_inline=False,
        related_name=None, limit_choices_to=None, lookup_overrides=None):
        try:
            to._meta
        except AttributeError: # to._meta doesn't exist, so it must be RECURSIVE_RELATIONSHIP_CONSTANT
            assert isinstance(to, basestring), "'to' must be either a model, a model name or the string %r" % RECURSIVE_RELATIONSHIP_CONSTANT
        self.to, self.field_name = to, field_name
        self.num_in_admin, self.edit_inline = num_in_admin, edit_inline
        self.min_num_in_admin, self.max_num_in_admin = min_num_in_admin, max_num_in_admin
        self.num_extra_on_change, self.related_name = num_extra_on_change, related_name
        if limit_choices_to is None:
            limit_choices_to = {}
        self.limit_choices_to = limit_choices_to
        self.lookup_overrides = lookup_overrides or {}
        self.multiple = True

    def get_related_field(self):
        "Returns the Field in the 'to' object to which this relationship is tied."
        return self.to._meta.get_field(self.field_name)

class OneToOneRel(ManyToOneRel):
    def __init__(self, to, field_name, num_in_admin=0, edit_inline=False,
        related_name=None, limit_choices_to=None, lookup_overrides=None):
        self.to, self.field_name = to, field_name
        self.num_in_admin, self.edit_inline = num_in_admin, edit_inline
        self.related_name = related_name
        if limit_choices_to is None:
            limit_choices_to = {}
        self.limit_choices_to = limit_choices_to
        self.lookup_overrides = lookup_overrides or {}
        self.multiple = False

class ManyToManyRel(object):
    def __init__(self, to, num_in_admin=0, related_name=None,
        limit_choices_to=None, symmetrical=True):
        self.to = to
        self.num_in_admin = num_in_admin
        self.related_name = related_name
        if limit_choices_to is None:
            limit_choices_to = {}
        self.limit_choices_to = limit_choices_to
        self.edit_inline = False
        self.symmetrical = symmetrical
        self.multiple = True
