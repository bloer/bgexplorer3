---
title: Concepts
nav_order: 3
---

# Concepts
{: .no_toc }

1. TOC
{:toc}

A background model predicts what a detector will see from the radioactivity
of everything around it. Background Explorer builds that prediction from three
kinds of input, and combines them for you:

```
  Component            EmissionSource            HitEfficiency
  "copper can"    ×    "U-238, 12 mBq/kg"   ×    "counts in ROI per decay
  mass 2 kg            (from an assay)            of U-238 in the can"
                                 │
                                 ▼
                SourceTerm: one source on one component,
                at one place in the detector
                                 │
                                 ▼
                Results: rates, ROI counts and spectra,
                summed over the whole model
```

## Components and assemblies

A **component** is a physical part: a cable, a vacuum can, a PCB. It has a
name, a material, and geometry: mass, volume, surface areas and lengths.

An **assembly** is a component made of other components. Each child is
*placed* in the assembly, with a **weight**, the number of copies. An
assembly's mass, volume and surface areas are the weighted sums of its
children's. Assemblies can contain other assemblies, so the whole detector is a
tree with (typically) one root assembly at the top. One could maintain two
separate or partially overlapping hierarchies to represent e.g. best- and worst-case
scenarios, but this ismore naturally done with two distinct versions.

In relation to GEANT4, a component is a "logical volume" with material and mass.
A **placement** is a "physical volume" specifying where and how many of each
component is placed in the assemblies "mother logical volume"

## Emission specs and sources

An **emission spec** is a convenience grouping of
**emission sources**, each an isotope (or other source name) with a rate, e.g.
`U-238: 12 +3 -2 mBq/kg`, or an upper limit like `< 5 mBq/kg`.

A component lists the specs that apply to it. One spec, say an assay of a
batch of copper, is usually shared by every component made of that copper.

The rate's units decide what it is multiplied by: Bq/kg by the component's
mass, Bq/m² by its surface area, Bq by nothing (for e.g. 'per-piece' normalized results),
and so on. Concentrations like
`ppb` of U-238 are converted to activities automatically for recognized isotopes.

There are several kinds of spec:

| Kind | Use |
|------|-----|
| Emission spec | the most generic type, rates entered by hand, e.g. estimates or targets |
| Assay | a material assay, with sample, measurement and publication details; can be imported from radiopurity.org |
| Radon exposure | Pb-210 plate-out calculated from a history of radon exposure |
| Cosmogenic activation | activation products calculated from a material's activation rates and an exposure history |

Components have a history tracking feature, but at present this is completely
separate from exposure histories.

See [Building a model](model.html) for details.

## Hit efficiencies

A **hit efficiency** is the detector's response to one source in one place,
usually from a Monte Carlo simulation: for example, the number of events in
a region of interest per decay of U-238 in the copper can. It can have:

- **scalars**: named values with units, e.g. counts per decay in an energy
  window;
- **spectra**: histograms of the response, e.g. the energy spectrum per decay.

Each hit efficiency is labelled with the `source` it simulated, e.g. `U238`,
and a `location`, the name of the volume it was simulated in, e.g. `CuCan`. It
may also give a `material`.

The 'efficiency' part of the term is important for getting units correct.
A 'component × source' typically has units like `decays / second`, so the
hit efficiency must be normalized to `counts / decay`.

## SourceTerms: matching it all up

For every component, every source in its specs, and every place the component
appears in the assembly tree, Background Explorer makes a **SourceTerm**. Its
emission rate is the source's rate, times the component's mass (or area, etc.),
times the weights of every placement above it.

Each SourceTerm is matched to the hit efficiencies whose:

- `source` is the source's name;
- `location` is the SourceTerm's location: by default the component's name, or
  a location set on the component, its placement, or an override;
- `material` is the component's material, or blank.

The SourceTerm's results are its emission rate times each matching hit
efficiency's scalars and spectra. A component, an assembly, or the whole
detector's results are the sums of its SourceTerms.

{: .important }
Source names are matched to hit efficiencies **exactly**: `U238` and `U-238`
are different. Use the same spelling in your specs as in your simulations, or
see [Location and source matching](model.html#matching-hit-efficiencies).

SourceTerms are kept up to date automatically when you edit anything. The
**Source Terms** tab on a component lists them, and shows which hit efficiencies
each one matched.

## Results

The **Results** of a component or assembly are the sums of its SourceTerms'
results: background rates, counts in regions of interest, and spectra. The
results page breaks them down by component, isotope, material and source
category, and can be filtered interactively. See [Results](results.html).

## Uncertainties

Every rate and efficiency can carry an (asymmetric) uncertainty or be an upper
limit, and correlations are tracked: two components using the same assay have
correlated uncertainties. See [Uncertainties](uncertainties.html).
It's rare to enter asymmetric uncertainties directly, but they come up often
when trying to add a value to an upper limit.

## Versions

The whole model lives in a **version**: a branch you can edit, or a read-only
tag. Versions can be compared, and documents copied or whole versions merged
between them. Each version has its own [settings](settings.html). See
[Versions](versions.html). In the web interface, there is always a single
"active" version, displayed on the navbar next to the 'Index' link. This is
similar to the active branch of a git repository. There is no "commit" concept,
but edits always apply to the active version only; the same object on a
different version remains unchanged.
