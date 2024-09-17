from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSource, EmissionSpec
from bgexplorer.models.assay import Assay, PublicationInfo, SampleInfo, MeasurementInfo, MeasurementResult
from bgexplorer.models.settings import get_settings, HitEffConfig
from bgexplorer.models.versioncontrol import create_version, delete_version
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import CalculatedResults, find_sourceterms
import importlib.resources
import os
import logging
import tarfile
log = logging.getLogger(__name__)

def populate_example(version_tag='examples/qis', clean: bool = False,
                     print_results: bool = False):
    # TODO: add cosmics and environmental gammas
    log.info(f"Populating 'qis' example on branch '{version_tag}'")
    log.debug("Creating branch")
    if clean:
        try:
            delete_version(version_tag)
        except KeyError:
            pass

    # make sure version exists
    try:
        create_version(version_tag, editable=True,
                       description=("Example model of a superconducting "
                                    "device in a dilution refrigerator"))
    except KeyError:
        pass
    settings = get_settings(version_tag)
    settings.addsources = []
    settings.hiteffdbconfig.display_scalars = dict(
        countrate=HitEffConfig(display_unit='1/g/s'),
        countrate_1MeV=HitEffConfig(display_unit='1/g/s'),
        countrate_2MeV=HitEffConfig(display_unit='1/g/s'),
        dose=HitEffConfig(display_unit='keV/g/s'),
        )
    settings.save()
    log.debug("Saving hit efficiencies")
    # hiteff data
    with tarfile.open(importlib.resources.files('bgexplorer.application.examples').joinpath('qis_hiteffs.tar.gz')) as tar:
        for jsonfile in tar:
            jsondata = tar.extractfile(jsonfile).read()
            hiteff = HitEfficiency.from_json(jsondata, created=True)
            hiteff.version_tags = [version_tag]
            # hiteff.jsonfile = os.path.basename(str(jsonfile))
            hiteff.save()

    log.debug("Saving assays")
    # assays
    assays = [
        Assay(name="copper", version_tag=version_tag,
            publication = PublicationInfo(shortlabel="XENON100", reference="E. Aprile et al., Astropart. Phys., 35 (2011)",
                                          details="Table 1, row 5", url="http://dx.doi.org/10.1016/j.astropartphys.2011.06.001"),
            sample = SampleInfo(material='copper', vendor="Norddeutsche Affinerie"),
            measurement = MeasurementInfo(technique="HPGe", instrument="Gator", count_time='51.4 day', results=[MeasurementResult(mass='512 kg', isotopes=dict(
                Ra228='21±7 uBq/kg', Th228='21±7 uBq/kg', U238='70±20 uBq/kg', Ra226='70±20 uBq/kg', U235='3.4 uBq/kg', K40='23±6 uBq/kg', Co60='2±1 uBq/kg'))],
                ),
            sources=dict(Th232='21±7 uBq/kg', U238='70±20 uBq/kg', K40='23±6 uBq/kg', Pb210='40 mBq/kg', Activation='6.61 mBq/kg'),
            ),

        Assay(name="steel", sources=dict(
            U238='130 mBq/kg', Th232='2.4 mBq/kg', K40='10 mBq/kg', Co60='8.5 mBq/kg', Cs137='0.9 mBq/kg'),
            ),
        Assay(name='aluminum', sources=dict(
            U238='66 mBq/kg', Th232='200 mBq/kg', K40='2100 mBq/kg')),
        Assay(name='gold', sources=dict(
            U238='74 mBq/kg', Th232='18.5 mBq/kg', K40='146 mBq/kg')),
        Assay(name='lead', sources=dict(
            U238='40 uBq/kg', Th232='5 uBq/kg', K40='10 uBq/kg', Pb210='200 Bq/kg')),
        Assay(name='Al-Si bonding wire', sources=dict(
            U238='107 mBq/kg', Th232='370 mBq/kg', K40='101 mBq/kg')),
        Assay(name="brass", sources=dict(
            U238='4.9 mBq/kg', Th232='3.5 mBq/kg', K40='40 mBq/kg', Cs137='2.6 mBq/kg', Activation='6.61 mBq/kg', Pb210='40 mBq/kg'),
            ),
        Assay(name='mumetal', sources=dict(
            U238='20 mBq/kg', Th232='7 mBq/kg', K40='15 mBq/kg')),
        Assay(name='BeCu D-sub pins', sources=dict(
            U238='2.39 mBq/kg', Th232='0.79 mBq/kg', K40='22.7 mBq/kg', Activation='6.61 mBq/kg', Pb210='40 mBq/kg')),
        Assay(name='alumina', sources=dict(
            U238='4.9383 Bq/kg', Th232='65.85 mBq/kg', K40='588 mBq/kg')),
        Assay(name='Rogers TMM10', sources=dict(
            U238='28.85 Bq/kg', Th232='4.5 Bq/kg', K40='17.5 Bq/kg')),
        Assay(name='Rogers RO4350B', sources=dict(
            U238='11.7 Bq/kg', Th232='13.3 Bq/kg', K40='9 Bq/kg')),
        Assay(name='SMA connector', sources=dict(
            U238='23 Bq/kg', Th232='1.8 Bq/kg')),
        Assay(name='semirigid coax cable', sources=dict(
            U238='0.4 mBq/kg', Th232='0.15 mBq/kg')),
        Assay(name='indium', sources=dict(
            In115='249.63 Bq/kg')),
        Assay(name='isolator', sources=dict(
            U238='240 mBq/kg', Th232='190 mBq/kg', K40='2000 mBq/kg', Cs137='500 mBq/kg')),
        Assay(name='HEMT', sources=dict(
            U238='1000 mBq/kg', Th232='890 mBq/kg', K40='10000 mBq/kg', Cs137='210 mBq/kg')),
        Assay(name='K&L filter', sources=dict(
            U238='9 mBq/kg', Th232='23 mBq/kg', K40='100 mBq/kg', Co60='5 mBq/kg', Cs137='1.9 mBq/kg')),
        Assay(name='attenuator', sources=dict(
            U238='200 mBq/kg', Th232='52 mBq/kg', K40='140 mBq/kg', Cs137='13 mBq/kg')),
    ]

    for assay in assays:
        assay.version_tags = [version_tag]
        assay.save()

    log.debug("Saving components")
    components = [
        Component(name='interposer RO4350B', mass='372 mg', specs=[Assay.objects.get(name='Rogers RO4350B')], location='Interposer board'),
        Component(name='interposer alumina', mass='0.00078 kg', location='Interposer board', specs=[Assay.objects.get(name='alumina')]),
        # Component(name='interposer kapton', mass='0.000284 kg', location='Interposer board', specs=[Assay.objects.get(name='kapton')]),
        Component(name='interposer TMM10', mass='0.000554 kg', location='Interposer board', specs=[Assay.objects.get(name='Rogers TMM10')]),
        Component(name='chip wirebonds', mass='1.00E-07 kg', location='Interposer board', specs=[Assay.objects.get(name='Al-Si bonding wire')]),
        Component(name='package wirebonds', mass='1.00E-07 kg', location='Interposer board', specs=[Assay.objects.get(name='gold')]),
        Component(name='package fasteners', mass='3.00E-04 kg', location='Package', specs=[Assay.objects.get(name='brass')]),
        Component(name='package', mass='0.1 kg', location='Package', specs=[Assay.objects.get(name='copper')]),
        Component(name='inner coax connectors', mass='2.60E-03 kg', location='Package Connector Inside', specs=[Assay.objects.get(name='SMA connector')]),
        Component(name='outer coax connectors', mass='2.60E-03 kg', location='Package Connector Outside', specs=[Assay.objects.get(name='SMA connector')]),
        Component(name='coax cable near package', mass='0.001 kg', location='Experiment stage', specs=[Assay.objects.get(name='semirigid coax cable')]),
        Component(name='experiment stage', mass='1.8 kg', location='Experiment stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='shield copper', mass='1 kg', location='Experiment shield', specs=[Assay.objects.get(name='copper')]),
        Component(name='shield aluminum', mass='1 kg', location='Experiment shield', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='shield cryoperm', mass='1 kg', location='Experiment shield', specs=[Assay.objects.get(name='mumetal')]),
        Component(name='MXC RF feedthroughs', mass='2.60E-03 kg', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='SMA connector')]),
        Component(name='MXC DC feedthroughs', mass='0.33 g', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='BeCu D-sub pins')]),
        # Component(name='RF circuitry', mass='1 kg', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='resistor')]),
        Component(name='MC Stage', mass='4.6 kg', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='CP Stage', mass='3.3 kg', location='Cold Plate Stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='ST Stage', mass='5.9 kg', location='Still Stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='4K Stage', mass='8.7 kg', location='4K Stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='50K Stage', mass='5.1 kg', location='50K Stage', specs=[Assay.objects.get(name='copper')]),
        Component(name='Vacuum Flange', mass='21 kg', location='Vacuum Flange', specs=[Assay.objects.get(name='steel')]),
        Component(name='Upper Vacuum Can', mass='9.1 kg', location='Upper Vacuum Can', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='Lower Vacuum Can', mass='12 kg', location='Lower Vacuum Can', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='Upper 50K Can', mass='1.7 kg', location='Upper 50K Can', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='Lower 50K Can', mass='4 kg', location='Lower 50K Can', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='4K Can', mass='4.1 kg', location='4K Can', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='Still Can', mass='6.3 kg', location='Still Can', specs=[Assay.objects.get(name='copper')]),
        Component(name='gold plating', mass='0.1 kg', specs=[Assay.objects.get(name='gold')]),
        # Component(name='Lead Shield Pb210', mass='1.8876344 kg', location='Lead Shield', specs=[Assay.objects.get(name='lead Pb210')]),
        Component(name='Lead Shield', mass='6148 kg', location='Lead Shield', specs=[Assay.objects.get(name='lead')]),
        Component(name='Lead Shield Lower Support', mass='118.8 kg', location='Lead Shield Support', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='Lead Shield Upper Support', mass='22 kg', location='Lead Shield Top Support', specs=[Assay.objects.get(name='aluminum')]),
        Component(name='bump bonds', mass='2.00E-08 kg', location='Bump bonds', specs=[Assay.objects.get(name='indium')]),
        Component(name='isolator', mass='0.145 kg', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='isolator')]),
        Component(name='HEMT', mass='0.017 kg', location='4K Stage', specs=[Assay.objects.get(name='HEMT')]),
        Component(name='cryo filters', mass='0.015 kg', location='Package Connector Outside', specs=[Assay.objects.get(name='K&L filter')]),
        Component(name='cryo attenuator', mass='0.005 kg', location='Mixing Chamber Stage', specs=[Assay.objects.get(name='attenuator')]),
        Component(name='Environment', location='Environment', sources=[EmissionSource(name='Gammaflux', rate='7.02095 1/cm**2/s', multiplier='none', category='environment')]),
    ]
    for component in components:
        component.version_tags = [version_tag]
        component.save()

    def placements(children):
        return [Placement(component=Component.select_version(version_tag).get(name=child),
                          weight=weight)
                for child, weight in children]

    log.debug("Saving assemblies")
    Assembly(name='50K Can', version_tag=version_tag, children=placements([
        ('Upper 50K Can', 1), ('Lower 50K Can', 1)])).save()
    Assembly(name='Vacuum Can', version_tag=version_tag, children=placements([
        ('Upper Vacuum Can', 1), ('Lower Vacuum Can', 1)])).save()

    readout = Assembly(name='readout', version_tag=version_tag,
                       children=placements([
                           ('chip wirebonds', 10),
                           ('package fasteners', 10),
                           ('package', 1),
                           ('experiment stage', 1),
                           ('shield copper', 1),
                           ('shield aluminum', 1),
                           ('shield cryoperm', 1),
                           ('coax cable near package', 10),
                           ('MXC RF feedthroughs', 10),
                           ('MXC DC feedthroughs', 100),
                           ('isolator', 10),
                           ('HEMT', 10),
                           ('cryo filters', 10),
                           ('cryo attenuator', 10),
                           ]),
                       ).save()
    goldplating = Component.select_version(version_tag).get(name='gold plating')
    fridge = Assembly(name='fridge', version_tag=version_tag,
                      children=placements([
                            ('MC Stage', 1),
                            ('CP Stage', 1),
                            ('ST Stage', 1),
                            ('4K Stage', 1),
                            ('50K Stage', 1),
                            ('Vacuum Flange', 1),
                            ('Vacuum Can', 1),
                            ('50K Can', 1),
                            ('4K Can', 1),
                            ('Still Can', 1)]) + [
                            Placement(component=goldplating, location='Experiment stage'),
                            Placement(component=goldplating, location='Mixing Chamber Stage'),
                            Placement(component=goldplating, location='Cold Plate Stage'),
                            Placement(component=goldplating, location='Still Can'),
                            Placement(component=goldplating, location='Still Stage'),
                            ],
                        ).save()
    fridge_readout = Assembly(name="fridge and readout", version_tag=version_tag,
                              components=[readout, fridge]).save()
    internals = Assembly(name='internals', version_tag=version_tag,
                         children=placements([
                            ('interposer RO4350B', 1),
                            ('interposer alumina', 1),
                            ('interposer TMM10', 1),
                            ('inner coax connectors', 10),
                            ('outer coax connectors', 10),
                            ('bump bonds', 1)]) + [
                            Placement(component=fridge_readout),
                            ],
                        ).save()
    shield = Assembly(name='shield', version_tag=version_tag,
                      children=placements([
                            ('Lead Shield', 1),
                            ('Lead Shield Lower Support', 1),
                            ('Lead Shield Upper Support', 1),
                            ]),
                       ).save()
    total = Assembly(name='total', version_tag=version_tag,
                     components=[internals, shield]+list(Component.objects(name='Environment')),
                     ).save()
    log.debug("Calculating results")
    result = CalculatedResults.for_component(total, save=True, save_intermediate=True)

    if print_results:
        # everything is relative to 'total'
        total = Assembly.select_version(version_tag).get(name='total')
        # print header
        scalars_cfg = settings.hiteffdbconfig.display_scalars
        print('#'*70)
        print('Component', end='')
        for name, cfg in scalars_cfg.items():
            print('\t', name, ' (',cfg.display_unit, ')', sep='', end='')
        print('\n', '-'*70, sep='')
        for component in Component.select_version(version_tag):
            sourceterms = find_sourceterms(component, relativeto=total)
            result = CalculatedResults.for_component(component, relativeto=total)
            if result is not None:
                vals = ['{:.3g}'.format(result.scalars[k].to(cfg.display_unit).m)
                        for k, cfg in scalars_cfg.items()]
            else:
                vals = ['' for _ in scalars_cfg]
            print('\t'.join([component.name] + vals))




if __name__ == '__main__':
    import sys
    import mongoengine
    logging.basicConfig(level=logging.DEBUG)
    uri = sys.argv[1] if len(sys.argv) > 1 else None
    mongoengine.connect(host=uri)
    populate_example(clean=True, print_results=True)






