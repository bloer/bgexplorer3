import mongoengine as me
from ..models.verdoc import VersionedDocument
from ..models.fields import get_fromstr

def input_type(field):
    """ Determine the <input> type for the given field """
    if hasattr(field, 'input_type'):
        return field.input_type
    if (isinstance(field, (me.ObjectIdField, ))
            or getattr(field, 'hidden', False)):
        return 'hidden'
    if isinstance(field, me.ReferenceField):
        return 'reference'
    if isinstance(field, me.EnumField) or field.choices:
        return 'select'
    if isinstance(field, me.EmailField):
        return 'email'
    if isinstance(field, me.URLField):
        return 'url'
    if isinstance(field, (me.IntField, me.FloatField)):
        return 'number'
    if isinstance(field, me.DateField):
        return 'date'
    if isinstance(field, me.DateTimeField):
        return 'datetime-local'
    if isinstance(field, me.BooleanField):
        return 'checkbox'
    return 'text'

def input_value(value):
    """ Determine the <input> value for a field value """
    if isinstance(value, me.Document):
        return getattr(value, 'original_id', None) or value.id
    if isinstance(value, me.EmbeddedDocument):
        return getattr(value, 'id', None) or value
    return get_fromstr(value) or value


LISTFIELDS_KEY = '_listfields'


def update_object(obj, form, prefix=None, index=0):
    """ Update the fields of the document object from the values in the form

    Fields not present in the form are left unchanged. Since an empty list
    submits no values, list fields are only updated if they have values or
    are named in the form's `LISTFIELDS_KEY` entries
    """
    listfields = form.getlist(LISTFIELDS_KEY)
    for fieldname in obj._fields_ordered:
        field = obj._fields[fieldname]
        fullfieldname = '.'.join([prefix, fieldname]) if prefix else fieldname
        value = None
        if isinstance(field, me.EmbeddedDocumentField):
            currentval = getattr(obj, fieldname, None)
            if not isinstance(currentval, me.EmbeddedDocument):
                currentval = field.document_type()
            value = update_object(currentval, form, prefix=fullfieldname,
                                  index=index)
            # TODO: handle null EmbeddedDocuments
        elif isinstance(field, me.EmbeddedDocumentListField):
            # TODO: can't nest lists or this will break
            # the EmbeddedDocument must have a default-constructor
            # use the first subfield to determine the list length
            doctype = field.field.document_type
            firstfield = '.'.join([fullfieldname, doctype._fields_ordered[0]])
            if firstfield not in form and fullfieldname not in listfields:
                continue
            value = [update_object(doctype(), form, prefix=fullfieldname,
                                   index=index)
                     for index in range(len(form.getlist(firstfield)))]
        elif isinstance(field, me.ListField):
            if fullfieldname not in form and fullfieldname not in listfields:
                continue
            value = form.getlist(fullfieldname, type=field.field.to_python)
        elif fullfieldname not in form:
            continue
        else:
            try:
                value = form.getlist(fullfieldname, type=field.to_python)[index]
            except IndexError:
                value = None
        # sometimes an empty string is a default that shouldn't be set, like id
        # sometimes the user will actually be nullifying something
        # how do we tell the difference?  Make a special default value?
        if value == '':
            delattr(obj, fieldname)
        else:
            setattr(obj, fieldname, value)
    return obj

