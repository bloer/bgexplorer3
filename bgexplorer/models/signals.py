import mongoengine
from .component import Component, Assembly
from .emissionspec import EmissionSpec
from .hiteff import HitEfficiency
from .sourceterm import SourceTerm, CalculatedResults, find_sourceterms
from . import settings
import logging
log = logging.getLogger(__name__)

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
    settings.touch(component.active_version)


def update_component(sender, document, **kwargs):
    component = document
    log.debug("update_component for %s", component.name)
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
        update_parent(parent, component, sourceterms)
    # the generic post_save signal may have run before the sourceterms
    # were updated
    settings.touch(component.active_version)
    return sourceterms

def update_parent(assembly, component, child_sourceterms, scope=()):
    """ Find or create SourceTerms in `assembly` for each placement of
    `component`, then recurse up the assembly tree.

    `child_sourceterms` must be the complete set of SourceTerms rooted at
    `component` below the placement path `scope` (a list of sets of
    placement ids). Any other SourceTerms in `assembly` along that path
    are deleted.
    """
    placements = [p for p in assembly.children
                  if p.component.original_id == component.original_id]
    sourceterms = [SourceTerm.from_placement(st, assembly, placement)
                   for placement in placements
                   for st in child_sourceterms]
    scope = [{p.id for p in placements}] + list(scope)
    # remove any that didn't match
    SourceTerm.objects(
        version_tags=assembly.active_version,
        assemblyRoot=assembly,
        id__nin=[st.id for st in sourceterms],
        __raw__={f'placement_ids.{i}': {'$in': list(ids)}
                 for i, ids in enumerate(scope)},
        ).delete()
    for parent in assembly.find_parents():
        update_parent(parent, assembly, sourceterms, scope)
    return sourceterms


def update_assembly(sender, document, component=None, placement=None,
                    **kwargs):
    assembly = document
    sourceterms = []
    updated = set()
    for child in assembly.children:
        if ((component and child.component.id != component.id) or
                (placement and child.id != placement.id)):
            continue
        updated.add(child.id)
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

    # if only some children were updated, limit deletions further up the
    # tree to paths through those children
    scope = [updated] if (component or placement) else []
    for parent in assembly.find_parents():
        update_parent(parent, assembly, sourceterms, scope)
        #update_assembly(sender=None, document=parent, component=assembly)
    settings.touch(assembly.active_version)


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


def clear_results(sourceterm_ids):
    """ Delete the cached CalculatedResults that include any of the
    SourceTerms with the given ids
    """
    CalculatedResults.objects(sources__in=list(sourceterm_ids)).delete()


def before_delete_hiteff(sender, document, **kwargs):
    # the reverse delete rule only pulls us from the SourceTerms
    document._sourceterms = list(find_sourceterms(document)
                                 .scalar('original_id'))


def after_delete_hiteff(sender, document, **kwargs):
    hiteff = document
    ids = getattr(hiteff, '_sourceterms', [])
    sourceterms = SourceTerm.select_version(hiteff.active_version)(
        original_id__in=ids)
    for st in sourceterms:
        # save to recalculate livetimes without us
        st.save()
    clear_results(sourceterms.scalar('id'))
    settings.touch(hiteff.active_version)


def update_hiteff(sender, document, **kwargs):
    hiteff = document
    # reverse the usual hiteff query to find all SourceTerms that would match
    matches = SourceTerm.select_version(hiteff.active_version)(
        hiteffs_auto=True,
        source__name=hiteff.source,
        location=hiteff.location,
        )
    if hiteff.material:
        matches = matches(material=hiteff.material)
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
    # the values may have changed without changing which sourceterms match
    clear_results(find_sourceterms(hiteff).scalar('id'))

    # update default units
    vsettings = settings.get_settings(hiteff.active_version)
    vsettings.hiteffdbconfig.update_from(hiteff)
    # call update rather than save to bypass cleaning and post-save signals
    vsettings.update(set__hiteffdbconfig=vsettings.hiteffdbconfig)
    settings.touch(hiteff.active_version)


def post_save(sender, document, **kwargs):
    """ Dispatch on the document's class. Connecting with `sender=` would
    only match that exact class, not its subclasses (e.g. Assay)
    """
    if isinstance(document, Assembly):
        update_assembly(sender, document, **kwargs)
    elif isinstance(document, Component):
        update_component(sender, document, **kwargs)
    elif isinstance(document, EmissionSpec):
        update_emissionspec(sender, document, **kwargs)
    elif isinstance(document, HitEfficiency):
        update_hiteff(sender, document, **kwargs)


def pre_delete(sender, document, **kwargs):
    if isinstance(document, Component):
        delete_component(sender, document, **kwargs)
    elif isinstance(document, HitEfficiency):
        before_delete_hiteff(sender, document, **kwargs)


def post_delete(sender, document, **kwargs):
    if isinstance(document, HitEfficiency):
        after_delete_hiteff(sender, document, **kwargs)


mongoengine.signals.post_save.connect(post_save)
mongoengine.signals.pre_delete.connect(pre_delete)
mongoengine.signals.post_delete.connect(post_delete)
