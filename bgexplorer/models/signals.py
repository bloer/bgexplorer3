from mongoengine import signals
from .component import Component, Assembly
from .sourceterm import SourceTerm

"""
signals that refer to multiple classes are here to prevent circular imports
"""


def delete_component(sender, document, **kwargs):
    if document is None or not isinstance(document, Component):
        return
    component = document
    # find all SourceTerms referencing us and delete
    qs = SourceTerm.select_version(component.active_version)
    # TODO: do this with an OR?
    qs(assemblyPath__component=component).delete()
    qs(assemblyRoot=component).delete()

    # find all Assemblies referencing us and remove from children
    qs = Assembly.select_version(component.active_version)
    for parent in qs(children__component=component):
        parent.children = [p for p in parent.children
                           if p.component.original_id != component.original_id]
        parent.save()


signals.pre_delete.connect(delete_component)
