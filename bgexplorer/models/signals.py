import mongoengine
from .component import Component, Assembly
from .emissionspec import EmissionSpec
from .hiteff import HitEfficiency
from .sourceterm import SourceTerm
from . import settings

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


def update_component(sender, document, **kwargs):
    component = document
    print("update_component for", component.name)
    # first, generate the list of all source terms for this component
    sourceterms = []
    for spec in list(component.specs) + [component]:
        for source in spec.sources:
            spec = spec if spec is not component else None
            sourceterms.append(SourceTerm.from_component_source(component,
                                                                source, spec)
                               )
    # remove any that didn't match
    SourceTerm.objects(
        version_tags=component.active_version,
        assemblyRoot=component,
        id__nin=[st.id for st in sourceterms],
        ).delete()
    # do we calculate the result now, or do it on demand?
    # update all assemblies containing us
    for parent in component.find_parents():
        #update_assembly(sender=None, document=parent, component=component)
        for st in sourceterms:
            update_parent(parent, st)
    return sourceterms

def update_parent(assembly, child_sourceterm):
    # find or create a sourceterm adder for each placement with this component
    sourceterms = []
    for child in assembly.children:
        if child.component.id != child_sourceterm.assemblyRoot.id:
            continue
        st = SourceTerm.from_placement(child_sourceterm, assembly, child)
        sourceterms.append(st)
    # remove any that didn't match
    SourceTerm.objects(
        version_tags=assembly.active_version,
        assemblyRoot=assembly,
        assemblyPath__0__component=child_sourceterm.assemblyRoot,
        id__nin=[st.id for st in sourceterms]).delete()
    print("update_parent", assembly.name, child_sourceterm.assemblyRoot.name, len(sourceterms))
    for parent in assembly.find_parents():
        for st in sourceterms:
            update_parent(parent, st)
    return sourceterms


def update_assembly(sender, document, component=None, placement=None,
                    **kwargs):
    assembly = document
    sourceterms = []
    for child in assembly.children:
        if ((component and child.component.id != component.id) or
                (placement and child.id != placement.id)):
            continue
        childterms = SourceTerm.select_version(assembly.active_version)(
            assemblyRoot=child.component,
            )
        for st in childterms:
            sourceterms.append(SourceTerm.from_placement(st, assembly, child))
    # remove any that didn't match
    query = SourceTerm.objects(version_tags=assembly.active_version,
                               assemblyRoot=assembly)
    if component:
        query = query(assemblyPath__0__component=component)
    query = query(id__nin=[st.id for st in sourceterms])
    query.delete()

    for parent in assembly.find_parents():
        for st in sourceterms:
            update_parent(parent, st)
        #update_assembly(sender=None, document=parent, component=assembly)


def update_placement(sender, document, **kwargs):
    # this doesn't work!!
    placement = document
    return update_assembly(sender=None, document=placement.parent,
                           placement=placement)


def update_emissionspec(sender, document, **kwargs):
    spec = document
    # find all components that own a reference to us and call update_component
    for component in Component.objects(version_tags=spec.active_version,
                                       specs=spec):
        update_component(sender=None, document=component)


def update_hiteff(sender, document, **kwargs):
    hiteff = document
    # reverse the usual hiteff query to find all SourceTerms that would match
    matches = SourceTerm.select_version(hiteff.active_version)(
        hiteffs_auto=True,
        source__name=hiteff.source,
        location=hiteff.location,
        )
    for st in matches(hiteffs__ne=hiteff):
        st.hiteffs.append(hiteff)
        # do save instead of push to force recalculation of livetimes
        # this is so stupidly inefficient, there's got to be a better way
        st.save()
    # remove ourselves from any sourceterm that no longer matches
    toremove = SourceTerm.select_version(hiteff.active_version)(
        hiteffs_auto=True,
        hiteffs=hiteff,
        original_id__nin=matches.scalar('original_id'),
        )
    for st in toremove:
        st.hiteffs = [h for h in st.hiteffs
                      if h.original_id != hiteff.original_id]
        st.save()

    # update default units
    vsettings = settings.get_settings(hiteff.active_version)
    vsettings.hiteffdbconfig.update_from(hiteff)
    # call update rather than save to bypass cleaning and post-save signals
    vsettings.update(set__hiteffdbconfig=vsettings.hiteffdbconfig)


mongoengine.signals.pre_delete.connect(delete_component)
mongoengine.signals.post_save.connect(update_component, sender=Component)
mongoengine.signals.post_save.connect(update_assembly, sender=Assembly)
mongoengine.signals.post_save.connect(update_emissionspec,
                                      sender=EmissionSpec)
mongoengine.signals.post_save.connect(update_hiteff, sender=HitEfficiency)
