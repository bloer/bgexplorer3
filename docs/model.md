---
title: Building a model
nav_order: 4
---

# Building a model
{: .no_toc }

1. TOC
{:toc}

This page describes each kind of document and its fields. Each version has
pages for its **Components**, **Sources** (emission specs), **Hit
Efficiencies** and **Activation** (activated materials), in the navigation bar. Every collection has an overview page with **New** and
**Import** buttons, and each item has a
view page with **Edit**, **Clone** and **Delete** buttons (hit efficiencies have no Clone). Editing needs the `editor`
role and a branch (not a tag); see [Users and roles](admin.html#users-and-roles).

A typical order of work:

1. Import or create the **hit efficiencies** from your simulations.
2. Create **emission specs** for your materials: assays, estimates, radon and
   cosmogenic exposure.
3. Create **components** with their materials and geometry, and attach specs.
4. Build **assemblies** out of components, up to one root assembly for the
   whole detector.
5. Check each component's **Source Terms** tab for SourceTerms that didn't
   match any hit efficiency, and fix names or locations.
6. Look at the [Results](results.html).

## Entering values

Quantities are typed as text with units. Uncertainties and limits are written
like this:

| You type | Meaning |
|----------|---------|
| `5 mBq/kg` | a value without uncertainty |
| `1.2 +- 0.3 mBq/kg` | symmetric uncertainty; `±` and `+/-` also work |
| `1.2 +0.3 -0.2 mBq/kg` | asymmetric uncertainty, upper first; additional spaces ok |
| `(1.2 ± 0.3)e-3 Bq/kg` | with a common exponent |
| `< 5 mBq/kg` | an upper limit at 90% CL |
| `< 5 (95%) mBq/kg` | an upper limit at another CL |

Any compatible unit works (`200 g` for a mass in kg, `5 cm**2` for an area).
Write powers as `**`, e.g. `Bq/m**2`. `ppm`, `ppb`, `ppt` and `ppq` are
available and automatically converted to `Bq/kg` based on the isotope.
The original string is retained for all display purposes rather than the
internal floating point representation.

{: .note }
The `1.2(3)` notation and negative values aren't supported yet.

## Components

| Field | Meaning |
|-------|---------|
| Name | required; can't contain `/` |
| Description, notes | free text |
| Material | free text, e.g. `copper`. Used to match hit efficiencies that give a material (for e.g. (alpha,n) neutron yields), and to group results |
| Mass, volume, inner and outer surface area, length, width, height | geometry, defaulting to 0. The surface area used for rates is inner + outer by default |
| Hit Efficiency Location | the `location` to match in the hit efficiency database; blank uses the component's name if not given in a parent Placement. Autocompletes from existing locations |
| Specs | the radiation emission specs that apply. Opens a search form. |
| Location overrides | send some sources to another location, see [below](#location-overrides) |
| Purchase info, material purchase info | optional vendor, part number, batch, order, dates… |
| History | handling records: date, location, duration, worker… |
| Attachments, extra metadata | anything else |

### Assemblies

An assembly has the same information fields, plus its **children**, but no
emission specs. Each child placement has:

- the **component** placed;
- a **weight**, the number of copies (any positive number, default 1);
- an optional **label**, to tell apart several placements of the same
  component; the placement's name is the label, or else the component's name;
- an optional **location**, the hit efficiency location for everything in this
  placement.

When saved, an assembly's mass, volume and surface areas are set to the
weighted sums of its children's. Weights multiply down the tree: a part placed
twice in a sub-assembly that is itself placed 4 times contributes 8 times.
Assemblies can't contain themselves.

### Owned specs

A spec can be *owned* by a single component. It is then only used by that
component, isn't offered for other components, and is deleted with it. Create
one with **New spec for this component** on the component's page. Owned specs
have an "own" badge, and are hidden from the emission spec overview unless you
choose **Show component-specific**. This is strictly a convenience to keep search
tables from getting cluttered with per-component information and can be completely
ignored.
Use **make specific** to replace a shared spec on one component with a private
copy, named `<spec> (<component>)` (the **make specific** button next to the
spec in the component's Radiation sources table), e.g. to give one component its own radon
history. Cloning a component also clones the specs it owns.

## Emission specs

Every spec has a name, description, comment, a **category**, an optional
**multiplier** and a list of **sources**. The spec's category is used for any
source without its own.

Categories: `target`, `estimate`, `assay`, `activation`, `dust`, `radon`,
`radon_emanation`, `environment`, `other`. They are used to group results.

Each source has a **name** (usually an isotope, e.g. `U238`), a **rate**, and
optionally its own category, multiplier, particle, spectrum and comment.

### Rate units and multipliers

A source's rate is multiplied by a property of the component, chosen from the
rate's units unless a multiplier is set:

| Multiplier | Rate units |
|------------|------------|
| mass | `Bq/kg`; concentrations like `ppb` |
| volume | `Bq/m**3` |
| surface (inner + outer), inner surface, outer surface | `Bq/m**2` (`surface` unless chosen) |
| length | `Bq/m` |
| none | `Bq`, or fluxes `1/cm**2/s`, `1/cm**2/s/sr` |

Use the spec's multiplier to choose e.g. the inner surface for all its
surface sources at once. Fluxes are primarily used to represent the ambient
laboratory backgrounds, and must match the units of the corresponding hit efficiency.
E.g., if the result unit is `counts/kg/keV/day` and the ambient gamma flux
measured in `1/cm**2/s/sr`, the hit efficiency must be normalized to
`(cm**2 sr)/(kg keV)`.

**Concentrations** (`ppb` etc., or a bare number as a fraction, so `1e-9` is
1 ppb) need the source's name to be an isotope. They are converted to
activities with the isotope's half-life and molar mass. For potassium, the
natural abundance of K-40 is applied: `K`, `natK`, `K-nat` and `Knat` all mean
natural potassium.

### Generated sources

Additional sources can be defined automatically based on the version [settings](settings.html#generated-sources). By default, every spec with U-238 also gets U-235 (by natural
abundance) and Ra-226 (in secular equilibrium). Generated sources are shown in
the spec. A source you enter with the same name takes precedence, and editing a
generated source turns it into your own. This is intended to handle models where
some materials are out of secular equilibrium, where the Ra-226 rate would be
entered separately, and otherwise it is generated automatically. Then all
simulation locations can be split between upper and lower chain.

### Assays

An assay is an emission spec with category `assay` and details of the
measurement, which are kept for reference and don't affect the results:

- **sample**: id, name, material, mass, vendor, part number, batch, owner…
- **request**: who asked for it, target sensitivity…
- **measurement**: technique, institution, instrument, dates, count time,
  operator, raw results;
- **publication**: reference, URL, details (e.g. "Table II, entry 45").

Measurement results are entered in a separate table than the sources used to
build the background model. The measurement may have multiple replicates,
isotopes that you don't want to track, or measure multiple isotopes in the same
chain, so the model values often must be interpreted.

One design goal of Background Explorer is to serve as an internal assay tracking
tool. This can be achieved by, e.g., creating a new "Assay Tracking" branch,
filling out the request section, followed by sample tracking and results, then
the result can be imported into the actual background model branch.

#### Importing from radiopurity.org

The **radiopurity.org** search on the emission specs pages finds published
assays and imports them as assays. Isotopes are normalized (`U` becomes
`U-238`, `Th` `Th-232`, `K` `K-40`), limits and errors are converted, and the
radiopurity.org record is kept with the assay. Values it can't convert, like
ranges or unknown units, are listed as warnings and not added as sources.

{: .important }
The import names isotopes like `U-238`. Make sure that matches the `source`
names of your hit efficiencies.

### Radon exposure

A radon exposure calculates the Pb-210 on a surface from the component's
history of exposure to radon. Its single source, Pb-210 in `Bq/m**2`, is
calculated; the default multiplier is the surface area, and must be one of the
surfaces.

The history is an ordered list of **periods**, each with:

| Field | Meaning |
|-------|---------|
| Description | e.g. cleanroom, lab |
| Mode | `free`: the radon is replenished, as in a room; `trapped`: a fixed amount is sealed in and decays |
| Radon level | `Bq/m**3`, may be uncertain |
| Duration | in days |
| Column height | height of air whose radon daughters plate out, default 10 cm |

Daughters deposit at radon level × column height. Pb-210 builds up during each
period and decays through the later ones; the result is its activity at the
end of the last period.

If you already know the surface activity, use a plain emission spec with
category `radon` instead.

### Cosmogenic activation

Cosmic rays activate materials while they are above ground. Two documents
describe it:

An **activated material** (its own collection) lists, for one material, the
**isotopes** produced and their **activation rate** at sea level, e.g.
`50 1/kg/day`, with a reference.

A **cosmogenic activation** spec, attached to a component, refers to an
activated material and has a list of exposure **periods**:

| Field | Meaning |
|-------|---------|
| Description, location | e.g. surface storage, flight |
| Duration | in days |
| Factor | activation rate relative to sea level: 1 at the surface, 0 deep underground; e.g. higher for flights |

A "cooldown" underground is a period with factor 0. The spec calculates one
source per isotope, in `Bq/kg` at the end of the last period.
Changing the activated material updates every spec that uses it.

{: .tip }
To plan with common estimates, e.g. "all of these parts get 10 days of
exposure", make one shared spec and attach it to all of them. As tracking
progresses, use **make specific** on a component to give it its own copy, and
edit that.

## Hit efficiencies

| Field | Meaning |
|-------|---------|
| Source | the simulated source, e.g. `U238`; matched exactly to source names |
| Location | the simulated volume, matched to SourceTerm locations |
| Material | optional; if set, only components of this material match |
| Normalization | `rate` (per decay per second, the default), `flux` (per 1/s/cm²), `flux_per_sr`, or `none` (absolutely normalized) |
| Scalars | named values with units, e.g. counts per decay in an energy window |
| Spectra | histograms as numpy binary, imported on the edit page |
| Simulation details | number of primaries, primary particle, spectrum and yield, bias weight, livetime, version, files, date… |

Most associations in Background Explorer are defined manually: attaching
emission specs to components, components to assemblies, activated materials
to activation specs. Hit Efficiencies are matched to Source Terms automatically
by keying on the source's name and component's location. It is therefore
critical to mainttain a consistent naming scheme for these fields
(e.g., U238 vs U-238 vs U vs 238U, capitalization).

The scalars' and spectra's display names, units and visibility are set in the
version [settings](settings.html#hit-efficiency-display). Regions of interest
defined there are evaluated from each hit efficiency's spectra and act as
extra scalars.

If **livetime** isn't given, a SourceTerm's simulated livetime is calculated as
primaries × bias weight / (emission rate × primary yield).

### Spectra

Spectra are managed on a hit efficiency's **edit** page: import one from a
file, rename it, or delete it. See [Importing data](import.html#spectra) for
file formats.

## Matching hit efficiencies

Each SourceTerm uses every hit efficiency whose `source` equals the source's
name, whose `location` equals the SourceTerm's location, and whose `material`
is the component's material or blank. Several matching hit efficiencies are
summed. This is intended to handle e.g. two separate simulations for neutrons
and gammas that should both be normalized to the same U238 rate. This means
that you **cannot** improve simulation uncertainties by uploading multiple hit
efficiency results for the same simulation parameters; they must be averaged
separately, and the new result will replace the original.

The location is decided from the most specific setting, starting at the
component itself and walking up to the root assembly:

1. a location override on the component for this spec or source;
2. the component's Hit Efficiency Location;
3. an override on the parent assembly for this placement;
4. the placement's location;
5. then the same on each assembly further up;
6. finally, the component's name.

This scheme allows correct hit efficiency assignment for multiply-placed items.
For example, if we have assayed a bag of screws, their radioactivity contribution
will be in multiple places determined by the parent assemblies' locations. It's
best to default to NOT assigning a location to each component, but instead
assign at the placement level.

The **Source Terms** tab shows each SourceTerm's location and where it came from,
e.g. "override on Tower" or "component name".

### Location overrides

An override sends the sources of one spec, or one source name, or both, to a
different location. For example, put the radon plate-out on a component's
surface at the `CuCanSurface` location while its bulk assay stays at `CuCan`.
As with other locations, this is an exact string match against the hit efficiency
table, so it's critical to observe consisten naming conventions.

On an assembly, an override can also be limited to one placement. When several
overrides match, the more specific one wins (a spec counts more than a source
name). Overrides for specs or placements that are removed go with them.

### Isotope names

Isotope names are recognized as `U238`, `U-238` or `238U`, and can have a suffix,
like `U238 lower`. These are treated as the same isotope for generated sources,
overrides, concentrations and activation. **Hit efficiency matching uses the exact string**, so pick one spelling for your simulations and specs.
