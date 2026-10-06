---
title: Version settings
nav_order: 8
---

# Version settings
{: .no_toc }

1. TOC
{:toc}

Each version has its own settings, shown on its overview and changed with
**Edit settings** (editors, on a branch). They are part of the version: they
are compared, merged and exported with it.

## Generated sources

**Automatic sources** add a source to every spec that has another one, e.g. to
fill in isotopes that are usually not measured directly. Each rule has:

| Field | Meaning |
|-------|---------|
| Source | the source that must be present, e.g. `U238` |
| New source | the source to add, e.g. `Ra226` |
| Ratio | the new source's rate relative to the source, default 1 |
| Ratio type | `rate`: a ratio of activities; `abundance`: a ratio of concentrations (atoms), converted with the half-lives |
| Comment | e.g. "Lower-chain U238 with secular equilibrium" |

The defaults are:

- U-238 → U-235, ratio 0.00725 by abundance (the natural abundance ratio);
- U-238 → Ra-226, ratio 1 by rate (secular equilibrium).

A source you enter in a spec with the same name takes precedence. Generated
sources are correlated with the source they come from.

## Hit efficiency display

**Hit Efficiency Configuration** chooses how results are shown.

### Display configuration

For each scalar and spectrum key found in the hit efficiencies:

| Field | Meaning |
|-------|---------|
| Display name | the name shown in results |
| Display unit | the unit shown, e.g. `1/g/s` or `counts/kg/day` |
| Description | shown with the result |
| Link spectrum | a spectrum related to this scalar |
| Hide | don't show this result |

Keys and display units are filled in from the hit efficiencies as they are
added. A unit that isn't compatible with existing hit efficiencies is
rejected. Similarly, adding a new hit efficiency with incompatible units is
rejected. If these aren't filled, the first inserted hit efficiency sets the units.

**Extra columns** lists extra hit efficiency fields, e.g. `material`, to show
as columns on the Hit Efficiencies overview.

### Regions of interest

**NOTE** ROI implementation is not tested yet; evaluate the scalars outside.

A region of interest (ROI) makes a new scalar from a spectrum, e.g. the rate
between 2400 and 2500 keV. It has:

| Field | Meaning |
|-------|---------|
| Spectrum | the spectrum key to use |
| Start, stop | the energy range, e.g. `2400 keV` |
| Mode | `average` (the default) or `integrate` over the range |
| Bin widths | whether to account for bin widths |
| Display name, display unit | as above; the name defaults to "*spectrum*, *mode* *start* to *stop*" |

ROIs are evaluated for every hit efficiency when the settings are saved, and
then act like any other scalar.

## Description and editability

The settings page also has the version's **description**. A tag's settings
can't be changed, and a tag can't be made editable again.

Settings can also be read and changed through the
[API](api.html#versions).
