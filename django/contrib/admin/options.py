from django import oldforms, template
from django import newforms as forms
from django.newforms.formsets import all_valid
from django.contrib.admin import widgets
from django.contrib.admin.util import get_deleted_objects
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import models
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render_to_response
from django.utils.html import escape
from django.utils.text import capfirst, get_text_list
from django.utils.translation import ugettext as _
from django.utils.encoding import force_unicode
import sets

class IncorrectLookupParameters(Exception):
    pass

def unquote(s):
    """
    Undo the effects of quote(). Based heavily on urllib.unquote().
    """
    mychr = chr
    myatoi = int
    list = s.split('_')
    res = [list[0]]
    myappend = res.append
    del list[0]
    for item in list:
        if item[1:2]:
            try:
                myappend(mychr(myatoi(item[:2], 16)) + item[2:])
            except ValueError:
                myappend('_' + item)
        else:
            myappend('_' + item)
    return "".join(res)

class AdminForm(object):
    def __init__(self, form, fieldsets, prepopulated_fields):
        self.form, self.fieldsets = form, fieldsets
        self.prepopulated_fields = [{'field': form[field_name], 'dependencies': [form[f] for f in dependencies]} for field_name, dependencies in prepopulated_fields.items()]

    def __iter__(self):
        for fieldset in self.fieldsets:
            yield BoundFieldset(self.form, fieldset)

    def first_field(self):
        for bf in self.form:
            return bf
            
    def _media(self):
        media = self.form.media
        for fs in self.fieldsets:
            media = media + fs.media
        return media
    media = property(_media)

class Fieldset(object):
    def __init__(self, name=None, fields=(), classes=(), description=None):
        self.name, self.fields = name, fields
        self.classes = u' '.join(classes)
        self.description = description

    def _media(self):
        from django.conf import settings
        if 'collapse' in self.classes:
            return forms.Media(js=['%sjs/admin/CollapsedFieldsets.js' % settings.ADMIN_MEDIA_PREFIX])
        return forms.Media()
    media = property(_media)
    
class BoundFieldset(object):
    def __init__(self, form, fieldset):
        self.form, self.fieldset = form, fieldset

    def __iter__(self):
        for field in self.fieldset.fields:
            yield BoundFieldline(self.form, field)

class BoundFieldline(object):
    def __init__(self, form, field):
        self.form = form # A django.forms.Form instance
        if isinstance(field, basestring):
            self.fields = [field]
        else:
            self.fields = field

    def __iter__(self):
        for i, field in enumerate(self.fields):
            yield BoundField(self.form, field, is_first=(i == 0))

    def errors(self):
        return u'\n'.join([self.form[f].errors.as_ul() for f in self.fields])

class BoundField(object):
    def __init__(self, form, field, is_first):
        self.field = form[field] # A django.forms.BoundField instance
        self.is_first = is_first # Whether this field is first on the line
        self.is_checkbox = isinstance(self.field.field.widget, forms.CheckboxInput)

    def label_tag(self):
        classes = []
        if self.is_checkbox:
            classes.append(u'vCheckboxLabel')
            contents = escape(self.field.label)
        else:
            contents = escape(self.field.label) + u':'
        if self.field.field.required:
            classes.append(u'required')
        if not self.is_first:
            classes.append(u'inline')
        attrs = classes and {'class': u' '.join(classes)} or {}
        return self.field.label_tag(contents=contents, attrs=attrs)

class BaseModelAdmin(object):
    """Functionality common to both ModelAdmin and InlineAdmin."""
    raw_id_fields = ()
    fields = None
    filter_vertical = ()
    filter_horizontal = ()
    def formfield_for_dbfield(self, db_field, **kwargs):
        """
        Hook for specifying the form Field instance for a given database Field
        instance.

        If kwargs are given, they're passed to the form Field's constructor.
        """
        # For ManyToManyFields with a filter interface, use a special widget.
        if isinstance(db_field, models.ManyToManyField) and db_field.name in (self.filter_vertical + self.filter_horizontal):
            kwargs['widget'] = widgets.FilteredSelectMultiple(db_field.verbose_name, (db_field.name in self.filter_vertical))
            return db_field.formfield(**kwargs)

        # For DateTimeFields, use a special field and widget.
        if isinstance(db_field, models.DateTimeField):
            return forms.SplitDateTimeField(required=not db_field.blank,
                widget=widgets.AdminSplitDateTime(), label=capfirst(db_field.verbose_name),
                help_text=db_field.help_text, **kwargs)

        # For DateFields, add a custom CSS class.
        if isinstance(db_field, models.DateField):
            kwargs['widget'] = widgets.AdminDateWidget
            return db_field.formfield(**kwargs)

        # For TimeFields, add a custom CSS class.
        if isinstance(db_field, models.TimeField):
            kwargs['widget'] = widgets.AdminTimeWidget
            return db_field.formfield(**kwargs)

        # For ForeignKey or ManyToManyFields, use a special widget.
        if isinstance(db_field, (models.ForeignKey, models.ManyToManyField)):
            if isinstance(db_field, models.ForeignKey) and db_field.name in self.raw_id_fields:
                kwargs['widget'] = widgets.ForeignKeyRawIdWidget(db_field.rel)
                return db_field.formfield(**kwargs)
            else:
                # Wrap the widget's render() method with a method that adds
                # extra HTML to the end of the rendered output.
                formfield = db_field.formfield(**kwargs)
                formfield.widget.render = widgets.RelatedFieldWidgetWrapper(formfield.widget.render, db_field.rel)
                return formfield

        # For any other type of field, just call its formfield() method.
        return db_field.formfield(**kwargs)

class ModelAdmin(BaseModelAdmin):
    "Encapsulates all admin options and functionality for a given model."
    __metaclass__ = forms.MediaDefiningClass
    
    list_display = ('__str__',)
    list_display_links = ()
    list_filter = ()
    list_select_related = False
    list_per_page = 100
    search_fields = ()
    date_hierarchy = None
    save_as = False
    save_on_top = False
    ordering = None
    prepopulated_fields = {}
    inlines = []

    def __init__(self, model):
        self.model = model
        self.opts = model._meta

    def __call__(self, request, url):
        # Check that LogEntry, ContentType and the auth context processor are installed.
        from django.conf import settings
        if settings.DEBUG:
            from django.contrib.contenttypes.models import ContentType
            from django.contrib.admin.models import LogEntry
            if not LogEntry._meta.installed:
                raise ImproperlyConfigured("Put 'django.contrib.admin' in your INSTALLED_APPS setting in order to use the admin application.")
            if not ContentType._meta.installed:
                raise ImproperlyConfigured("Put 'django.contrib.contenttypes' in your INSTALLED_APPS setting in order to use the admin application.")
            if 'django.core.context_processors.auth' not in settings.TEMPLATE_CONTEXT_PROCESSORS:
                raise ImproperlyConfigured("Put 'django.core.context_processors.auth' in your TEMPLATE_CONTEXT_PROCESSORS setting in order to use the admin application.")

        # Delegate to the appropriate method, based on the URL.
        if url is None:
            return self.changelist_view(request)
        elif url.endswith('add'):
            return self.add_view(request)
        elif url.endswith('history'):
            return self.history_view(request, unquote(url[:-8]))
        elif url.endswith('delete'):
            return self.delete_view(request, unquote(url[:-7]))
        else:
            return self.change_view(request, unquote(url))

    def _media(self):
        from django.conf import settings

        js = ['js/core.js', 'js/admin/RelatedObjectLookups.js']
        if self.prepopulated_fields:
            js.append('js/urlify.js')
        if self.opts.get_ordered_objects():
            js.extend(['js/getElementsBySelector.js', 'js/dom-drag.js' , 'js/admin/ordering.js'])
        if self.filter_vertical or self.filter_horizontal:
            js.extend(['js/SelectBox.js' , 'js/SelectFilter2.js'])
        
        return forms.Media(js=['%s%s' % (settings.ADMIN_MEDIA_PREFIX, url) for url in js])
    media = property(_media)
    
    def fieldsets(self, request):
        """
        Generator that yields Fieldset objects for use on add and change admin
        form pages.

        This default implementation looks at self.fields, but subclasses can
        override this implementation and do something special based on the
        given HttpRequest object.
        """
        if self.fields is None:
            default_fields = [f.name for f in self.opts.fields + self.opts.many_to_many if f.editable and not isinstance(f, models.AutoField)]
            yield Fieldset(fields=default_fields)
        else:
            for name, options in self.fields:
                yield Fieldset(name, options['fields'], classes=options.get('classes', '').split(' '), description=options.get('description'))

    def fieldsets_add(self, request):
        "Hook for specifying Fieldsets for the add form."
        return list(self.fieldsets(request))

    def fieldsets_change(self, request, obj):
        "Hook for specifying Fieldsets for the change form."
        return list(self.fieldsets(request))

    def has_add_permission(self, request):
        "Returns True if the given request has permission to add an object."
        opts = self.opts
        return request.user.has_perm(opts.app_label + '.' + opts.get_add_permission())

    def has_change_permission(self, request, obj):
        """
        Returns True if the given request has permission to change the given
        Django model instance.

        If `obj` is None, this should return True if the given request has
        permission to change *any* object of the given type.
        """
        opts = self.opts
        return request.user.has_perm(opts.app_label + '.' + opts.get_change_permission())

    def has_delete_permission(self, request, obj):
        """
        Returns True if the given request has permission to change the given
        Django model instance.

        If `obj` is None, this should return True if the given request has
        permission to delete *any* object of the given type.
        """
        opts = self.opts
        return request.user.has_perm(opts.app_label + '.' + opts.get_delete_permission())

    def queryset(self, request):
        """
        Returns a QuerySet of all model instances that can be edited by the
        admin site.
        """
        return self.model._default_manager.get_query_set()

    def queryset_add(self, request):
        """
        Returns a QuerySet of all model instances that can be edited by the
        admin site in the "add" stage.
        """
        return self.queryset(request)

    def queryset_change(self, request):
        """
        Returns a QuerySet of all model instances that can be edited by the
        admin site in the "change" stage.
        """
        return self.queryset(request)

    def save_add(self, request, model, form, formsets, post_url_continue):
        """
        Saves the object in the "add" stage and returns an HttpResponseRedirect.

        `form` is a bound Form instance that's verified to be valid.
        """
        from django.contrib.admin.models import LogEntry, ADDITION
        from django.contrib.contenttypes.models import ContentType
        opts = model._meta
        new_object = form.save(commit=True)

        if formsets:
            for formset in formsets:
                # HACK: it seems like the parent obejct should be passed into
                # a method of something, not just set as an attribute
                formset.instance = new_object
                formset.save()

        pk_value = new_object._get_pk_val()
        LogEntry.objects.log_action(request.user.id, ContentType.objects.get_for_model(model).id, pk_value, str(new_object), ADDITION)
        msg = _('The %(name)s "%(obj)s" was added successfully.') % {'name': opts.verbose_name, 'obj': new_object}
        # Here, we distinguish between different save types by checking for
        # the presence of keys in request.POST.
        if request.POST.has_key("_continue"):
            request.user.message_set.create(message=msg + ' ' + _("You may edit it again below."))
            if request.POST.has_key("_popup"):
                post_url_continue += "?_popup=1"
            return HttpResponseRedirect(post_url_continue % pk_value)
        if request.POST.has_key("_popup"):
            if type(pk_value) is str: # Quote if string, so JavaScript doesn't think it's a variable.
                pk_value = '"%s"' % pk_value.replace('"', '\\"')
            return HttpResponse('<script type="text/javascript">opener.dismissAddAnotherPopup(window, %s, "%s");</script>' % \
                (pk_value, str(new_object).replace('"', '\\"')))
        elif request.POST.has_key("_addanother"):
            request.user.message_set.create(message=msg + ' ' + (_("You may add another %s below.") % opts.verbose_name))
            return HttpResponseRedirect(request.path)
        else:
            request.user.message_set.create(message=msg)
            # Figure out where to redirect. If the user has change permission,
            # redirect to the change-list page for this object. Otherwise,
            # redirect to the admin index.
            if self.has_change_permission(request, None):
                post_url = '../'
            else:
                post_url = '../../../'
            return HttpResponseRedirect(post_url)

    def save_change(self, request, model, form, formsets=None):
        """
        Saves the object in the "change" stage and returns an HttpResponseRedirect.

        `form` is a bound Form instance that's verified to be valid.
        
        `formsets` is a sequence of InlineFormSet instances that are verified to be valid.
        """
        from django.contrib.admin.models import LogEntry, CHANGE
        from django.contrib.contenttypes.models import ContentType
        opts = model._meta
        new_object = form.save(commit=True)
        pk_value = new_object._get_pk_val()

        if formsets:
            for formset in formsets:
                formset.save()

        # Construct the change message. TODO: Temporarily commented-out,
        # as manipulator object doesn't exist anymore, and we don't yet
        # have a way to get fields_added, fields_changed, fields_deleted.
        change_message = []
        #if manipulator.fields_added:
            #change_message.append(_('Added %s.') % get_text_list(manipulator.fields_added, _('and')))
        #if manipulator.fields_changed:
            #change_message.append(_('Changed %s.') % get_text_list(manipulator.fields_changed, _('and')))
        #if manipulator.fields_deleted:
            #change_message.append(_('Deleted %s.') % get_text_list(manipulator.fields_deleted, _('and')))
        #change_message = ' '.join(change_message)
        if not change_message:
            change_message = _('No fields changed.')
        LogEntry.objects.log_action(request.user.id, ContentType.objects.get_for_model(model).id, pk_value, str(new_object), CHANGE, change_message)

        msg = _('The %(name)s "%(obj)s" was changed successfully.') % {'name': opts.verbose_name, 'obj': new_object}
        if request.POST.has_key("_continue"):
            request.user.message_set.create(message=msg + ' ' + _("You may edit it again below."))
            if request.REQUEST.has_key('_popup'):
                return HttpResponseRedirect(request.path + "?_popup=1")
            else:
                return HttpResponseRedirect(request.path)
        elif request.POST.has_key("_saveasnew"):
            request.user.message_set.create(message=_('The %(name)s "%(obj)s" was added successfully. You may edit it again below.') % {'name': opts.verbose_name, 'obj': new_object})
            return HttpResponseRedirect("../%s/" % pk_value)
        elif request.POST.has_key("_addanother"):
            request.user.message_set.create(message=msg + ' ' + (_("You may add another %s below.") % opts.verbose_name))
            return HttpResponseRedirect("../add/")
        else:
            request.user.message_set.create(message=msg)
            return HttpResponseRedirect("../")

    def add_view(self, request, form_url=''):
        "The 'add' admin view for this model."
        from django.contrib.admin.views.main import render_change_form
        model = self.model
        opts = model._meta
        app_label = opts.app_label

        if not self.has_add_permission(request):
            raise PermissionDenied

        if self.has_change_permission(request, None):
            # redirect to list view
            post_url = '../'
        else:
            # Object list will give 'Permission Denied', so go back to admin home
            post_url = '../../../'

        ModelForm = forms.form_for_model(model, formfield_callback=self.formfield_for_dbfield)

        inline_formsets = []
        if request.method == 'POST':
            form = ModelForm(request.POST, request.FILES)
            for FormSet in self.get_inline_formsets():
                inline_formset = FormSet(data=request.POST, files=request.FILES)
                inline_formsets.append(inline_formset)
            if all_valid(inline_formsets) and form.is_valid():
                return self.save_add(request, model, form, inline_formsets, '../%s/')
        else:
            form = ModelForm(initial=request.GET)
            for FormSet in self.get_inline_formsets():
                inline_formset = FormSet()
                inline_formsets.append(inline_formset)

        adminForm = AdminForm(form, list(self.fieldsets_add(request)), self.prepopulated_fields)
        media = self.media + adminForm.media
        for fs in inline_formsets:
            media = media + fs.media
            
        c = template.RequestContext(request, {
            'title': _('Add %s') % opts.verbose_name,
            'adminform': adminForm,
            'is_popup': request.REQUEST.has_key('_popup'),
            'show_delete': False,
            'media': media,
            'bound_inlines': [BoundInline(i, fs) for i, fs in zip(self.inlines, inline_formsets)],
        })
        return render_change_form(self, model, model.AddManipulator(), c, add=True)

    def change_view(self, request, object_id):
        "The 'change' admin view for this model."
        from django.contrib.admin.views.main import render_change_form
        model = self.model
        opts = model._meta
        app_label = opts.app_label

        try:
            obj = model._default_manager.get(pk=object_id)
        except model.DoesNotExist:
            # Don't raise Http404 just yet, because we haven't checked
            # permissions yet. We don't want an unauthenticated user to be able
            # to determine whether a given object exists.
            obj = None

        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404('%s object with primary key %r does not exist.' % (opts.verbose_name, escape(object_id)))

        if request.POST and request.POST.has_key("_saveasnew"):
            return self.add_view(request, form_url='../../add/')

        ModelForm = forms.form_for_instance(obj, formfield_callback=self.formfield_for_dbfield)

        inline_formsets = []
        if request.method == 'POST':
            form = ModelForm(request.POST, request.FILES)
            for FormSet in self.get_inline_formsets():
                inline_formset = FormSet(obj, request.POST, request.FILES)
                inline_formsets.append(inline_formset)

            if all_valid(inline_formsets) and form.is_valid():
                return self.save_change(request, model, form, inline_formsets)
        else:
            form = ModelForm()
            for FormSet in self.get_inline_formsets():
                inline_formset = FormSet(obj)
                inline_formsets.append(inline_formset)

        ## Populate the FormWrapper.
        #oldform = oldforms.FormWrapper(manipulator, new_data, errors)
        #oldform.original = manipulator.original_object
        #oldform.order_objects = []

        ## TODO: Should be done in flatten_data  / FormWrapper construction
        #for related in opts.get_followed_related_objects():
            #wrt = related.opts.order_with_respect_to
            #if wrt and wrt.rel and wrt.rel.to == opts:
                #func = getattr(manipulator.original_object, 'get_%s_list' %
                        #related.get_accessor_name())
                #orig_list = func()
                #oldform.order_objects.extend(orig_list)
                
        adminForm = AdminForm(form, self.fieldsets_change(request, obj), self.prepopulated_fields)        
        media = self.media + adminForm.media
        for fs in inline_formsets:
            media = media + fs.media
            
        c = template.RequestContext(request, {
            'title': _('Change %s') % opts.verbose_name,
            'adminform': adminForm,
            'object_id': object_id,
            'original': obj,
            'is_popup': request.REQUEST.has_key('_popup'),
            'media': media,
            'bound_inlines': [BoundInline(i, fs) for i, fs in zip(self.inlines, inline_formsets)],
        })
        return render_change_form(self, model, model.ChangeManipulator(object_id), c, change=True)

    def changelist_view(self, request):
        "The 'change list' admin view for this model."
        from django.contrib.admin.views.main import ChangeList, ERROR_FLAG
        opts = self.model._meta
        app_label = opts.app_label
        if not self.has_change_permission(request, None):
            raise PermissionDenied
        try:
            cl = ChangeList(request, self.model, self.list_display, self.list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given and
            # the 'invalid=1' parameter was already in the query string, something
            # is screwed up with the database, so display an error page.
            if ERROR_FLAG in request.GET.keys():
                return render_to_response('admin/invalid_setup.html', {'title': _('Database error')})
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')
        c = template.RequestContext(request, {
            'title': cl.title,
            'is_popup': cl.is_popup,
            'cl': cl,
        })
        c.update({'has_add_permission': c['perms'][app_label][opts.get_add_permission()]}),
        return render_to_response(['admin/%s/%s/change_list.html' % (app_label, opts.object_name.lower()),
                                'admin/%s/change_list.html' % app_label,
                                'admin/change_list.html'], context_instance=c)

    def delete_view(self, request, object_id):
        "The 'delete' admin view for this model."
        from django.contrib.contenttypes.models import ContentType
        from django.contrib.admin.models import LogEntry, DELETION
        opts = self.model._meta
        app_label = opts.app_label

        try:
            obj = self.model._default_manager.get(pk=object_id)
        except self.model.DoesNotExist:
            # Don't raise Http404 just yet, because we haven't checked
            # permissions yet. We don't want an unauthenticated user to be able
            # to determine whether a given object exists.
            obj = None

        if not self.has_delete_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404('%s object with primary key %r does not exist.' % (opts.verbose_name, escape(object_id)))

        # Populate deleted_objects, a data structure of all related objects that
        # will also be deleted.
        deleted_objects = [u'%s: <a href="../../%s/">%s</a>' % (force_unicode(capfirst(opts.verbose_name)), object_id, escape(str(obj))), []]
        perms_needed = sets.Set()
        get_deleted_objects(deleted_objects, perms_needed, request.user, obj, opts, 1)

        if request.POST: # The user has already confirmed the deletion.
            if perms_needed:
                raise PermissionDenied
            obj_display = str(obj)
            obj.delete()
            LogEntry.objects.log_action(request.user.id, ContentType.objects.get_for_model(self.model).id, object_id, obj_display, DELETION)
            request.user.message_set.create(message=_('The %(name)s "%(obj)s" was deleted successfully.') % {'name': force_unicode(opts.verbose_name), 'obj': force_unicode(obj_display)})
            return HttpResponseRedirect("../../")
        extra_context = {
            "title": _("Are you sure?"),
            "object_name": opts.verbose_name,
            "object": obj,
            "deleted_objects": deleted_objects,
            "perms_lacking": perms_needed,
            "opts": opts,
        }
        return render_to_response(["admin/%s/%s/delete_confirmation.html" % (app_label, opts.object_name.lower() ),
                                "admin/%s/delete_confirmation.html" % app_label ,
                                "admin/delete_confirmation.html"], extra_context, context_instance=template.RequestContext(request))

    def history_view(self, request, object_id):
        "The 'history' admin view for this model."
        from django.contrib.contenttypes.models import ContentType
        from django.contrib.admin.models import LogEntry
        model = self.model
        opts = model._meta
        action_list = LogEntry.objects.filter(object_id=object_id,
            content_type__id__exact=ContentType.objects.get_for_model(model).id).select_related().order_by('action_time')
        # If no history was found, see whether this object even exists.
        obj = get_object_or_404(model, pk=object_id)
        extra_context = {
            'title': _('Change history: %s') % force_unicode(obj),
            'action_list': action_list,
            'module_name': capfirst(opts.verbose_name_plural),
            'object': obj,
        }
        template_list = [
            "admin/%s/%s/object_history.html" % (opts.app_label, opts.object_name.lower()),
            "admin/%s/object_history.html" % opts.app_label,
            "admin/object_history.html"
        ]
        return render_to_response(template_list, extra_context, context_instance=template.RequestContext(request))

    def get_inline_formsets(self):
        inline_formset_classes = []
        for opts in self.inlines:
            inline = forms.inline_formset(self.model, opts.model, formfield_callback=opts.formfield_for_dbfield, fields=opts.fields, extra=opts.extra)
            inline_formset_classes.append(inline)
        return inline_formset_classes

class InlineModelAdmin(BaseModelAdmin):
    """
    Options for inline editing of ``model`` instances.

    Provide ``name`` to specify the attribute name of the ``ForeignKey`` from
    ``model`` to its parent. This is required if ``model`` has more than one
    ``ForeignKey`` to its parent.
    """
    def __init__(self, model, name=None, extra=3, fields=None, template=None, raw_id_fields=None):
        self.model = model
        self.opts = model._meta
        self.name = name
        self.extra = extra
        self.fields = fields
        self.template = template or self.default_template
        self.verbose_name = model._meta.verbose_name
        self.verbose_name_plural = model._meta.verbose_name_plural
        self.prepopulated_fields = {}
        self.raw_id_fields = raw_id_fields or ()

class StackedInline(InlineModelAdmin):
    default_template = 'admin/edit_inline_stacked.html'

class TabularInline(InlineModelAdmin):
    default_template = 'admin/edit_inline_tabular.html'

class BoundInline(object):
    def __init__(self, inline_admin, formset):
        self.inline_admin = inline_admin
        self.formset = formset
        self.template = inline_admin.template

    def __iter__(self):
        for form, original in zip(self.formset.change_forms, self.formset.get_inline_objects()):
            yield BoundInlineObject(self.formset, form, original, self.inline_admin)
        for form in self.formset.add_forms:
            yield BoundInlineObject(self.formset, form, None, self.inline_admin)

    def fields(self):
        return self.formset.form_class.base_fields.values()

    def verbose_name(self):
        return self.inline_admin.verbose_name

    def verbose_name_plural(self):
        return self.inline_admin.verbose_name_plural

class BoundInlineObject(object):
    def __init__(self, formset, form, original, inline_admin):
        self.formset = formset
        self.inline_admin = inline_admin
        self.base_form = form
        self.form = AdminForm(form, self.fieldsets(), inline_admin.prepopulated_fields)
        self.original = original

    def fieldsets(self):
        """
        Generator that yields Fieldset objects for use on add and change admin
        form pages.

        This default implementation looks at self.fields, but subclasses can
        override this implementation and do something special based on the
        given HttpRequest object.
        """
        if self.inline_admin.fields is None:
            default_fields = [f for f in self.base_form.base_fields]
            yield Fieldset(fields=default_fields)
        else:
            for name, options in self.opts.fields:
                yield Fieldset(name, options['fields'], classes=options.get('classes', '').split(' '), description=options.get('description'))

    def pk_field(self):
        return BoundField(self.base_form, self.formset._pk_field_name, False)

    def deletion_field(self):
        from django.newforms.formsets import DELETION_FIELD_NAME
        return BoundField(self.base_form, DELETION_FIELD_NAME, False)

    def ordering_field(self):
        from django.newforms.formsets import ORDERING_FIELD_NAME
        return BoundField(self.base_form, ORDERING_FIELD_NAME, False)