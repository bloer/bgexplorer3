import mongoengine
from bson.dbref import DBRef
from .component import Component, Assembly
from .emissionspec import EmissionSpec
from .cosmogenic import ActivatedMaterial, CosmogenicActivation
from .hiteff import HitEfficiency
from .sourceterm import (SourceTerm, find_sourceterms, sync_sourceterms,
                         placed_sourceterms)
from .verdoc import ref_id
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

    # specs that only it uses go with it
    for spec in EmissionSpec.select_version(component.active_version)(
            owner=component):
        # no need to update a component that is going away
        spec.owner_deleting = True
        spec.delete()

    # find all Assemblies referencing us and remove from children
    qs = Assembly.select_version(component.active_version)
    for parent in qs(children__component=component):
        parent.children = [p for p in parent.children
                           if p.component.original_id != component.original_id]
        parent.save()
    settings.touch(component.active_version, terms_only=True)


def delete_emissionspec(sender, document, **kwargs):
    """ Detach a deleted spec. Reverse delete rules aren't allowed inside
    the embedded documents that hold the references
    """
    spec = document
    if getattr(spec, 'owner_deleting', False):
        return
    for component in Component.select_version(spec.active_version)(
            spec_entries__spec=spec):
        component.spec_entries = [
            e for e in component.spec_entries
            if ref_id(e._data.get('spec')) != spec.original_id]
        component.save()


def update_component(sender, document, **kwargs):
    component = document
    log.debug("update_component for %s", component.name)
    # first, generate the list of all source terms for this component
    if any(isinstance(spec, DBRef) for spec in component.specs):
        log.warning("Component %s specs contain DBRefs: %s", component.name, component.specs)
    # why are specs sometimes being converted to DBRefs between save and update??
    version = component.active_version
    candidates = [SourceTerm(version_tag=version, assemblyRoot=component,
                             source=source, spec=spec)
                  for spec in component.specs
                  for source in getattr(spec, 'sources', [])]
    sync_sourceterms(version, candidates, SourceTerm.objects(
        version_tags=version, assemblyRoot=component))
    # update all assemblies containing us. These are always checked, even if
    # our SourceTerms didn't change, in case they missed a previous update
    for parent in component.find_parents():
        update_parent(parent, component)
    # the generic post_save signal may have run before the sourceterms
    # were updated
    settings.touch(version, terms_only=True)


def _sync_placements(assembly, placements, child_scope=(), restrict=True):
    """ Make the SourceTerms of `assembly` for each of the `placements` of its
    children match the current SourceTerms of the placed components below the
    path `child_scope` (a list of sets of placement ids). If not `restrict`,
    all other SourceTerms in `assembly` are removed too, otherwise only those
    along the same path. Returns the SyncResult and the scope of the
    SourceTerms of `assembly` that were synced.
    """
    version = assembly.active_version
    existing = SourceTerm.objects(version_tags=version, assemblyRoot=assembly)
    scope = [{p.id for p in placements}] + list(child_scope)
    if restrict:
        existing = existing(__raw__={f'placement_ids.{i}': {'$in': list(ids)}
                                     for i, ids in enumerate(scope)})
    result = sync_sourceterms(
        version, placed_sourceterms(assembly, placements, child_scope),
        existing)
    return result, scope


def update_parent(assembly, component, scope=()):
    """ Update the SourceTerms in `assembly` for each placement of
    `component`, then recurse up the assembly tree as long as something
    changed.

    `scope` is a list of sets of placement ids that limits which
    SourceTerms rooted at `component` were affected. Any other SourceTerms in
    `assembly` along that path are deleted.
    """
    placements = [p for p in assembly.children
                  if p.component.original_id == component.original_id]
    result, scope = _sync_placements(assembly, placements, scope)
    if result.changed:
        # SourceTerms further up depend only on these, so stop if none did
        for parent in assembly.find_parents():
            update_parent(parent, assembly, scope)


def update_assembly(sender, document, component=None, placement=None,
                    **kwargs):
    assembly = document
    placements = [child for child in assembly.children
                  if not ((component and child.component.id != component.id)
                          or (placement and child.id != placement.id))]
    # if only some children were updated, limit deletions to paths through
    # those children
    restrict = bool(component or placement)
    result, scope = _sync_placements(assembly, placements, (), restrict)
    # parents always get updated, in case they missed a previous update
    scope = scope if restrict else []
    for parent in assembly.find_parents():
        update_parent(parent, assembly, scope)
    settings.touch(assembly.active_version, terms_only=True)


def update_placement(sender, document, **kwargs):
    # this doesn't work!!
    placement = document
    return update_assembly(sender=None, document=placement.parent,
                           placement=placement)


def update_emissionspec(sender, document, **kwargs):
    spec = document
    # find all components that own a reference to us and call update_component
    for component in Component.objects(version_tags=spec.active_version,
                                       spec_entries__spec=spec):
        update_component(sender=None, document=component)


def update_activatedmaterial(sender, document, **kwargs):
    material = document
    # recalculate the activations of the material, which update their
    # components in turn
    for spec in CosmogenicActivation.select_version(
            material.active_version)(material=material):
        spec.save()


def delete_activatedmaterial(sender, document, **kwargs):
    # activations can't be calculated without their material
    for spec in CosmogenicActivation.select_version(
            document.active_version)(material=document):
        spec.delete()


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
    elif isinstance(document, ActivatedMaterial):
        update_activatedmaterial(sender, document, **kwargs)


def pre_delete(sender, document, **kwargs):
    if isinstance(document, Component):
        delete_component(sender, document, **kwargs)
    elif isinstance(document, EmissionSpec):
        delete_emissionspec(sender, document, **kwargs)
    elif isinstance(document, HitEfficiency):
        before_delete_hiteff(sender, document, **kwargs)
    elif isinstance(document, ActivatedMaterial):
        delete_activatedmaterial(sender, document, **kwargs)


def post_delete(sender, document, **kwargs):
    if isinstance(document, HitEfficiency):
        after_delete_hiteff(sender, document, **kwargs)


mongoengine.signals.post_save.connect(post_save)
mongoengine.signals.pre_delete.connect(pre_delete)
mongoengine.signals.post_delete.connect(post_delete)
