---
title: Uncertainties
nav_order: 7
---

# Uncertainties and correlations
{: .no_toc }

1. TOC
{:toc}

## Asymmetric uncertainties and upper limits

Rates, hit efficiencies and results are *asymmetric uncertainties*: a most
likely value (the mode) with separate lower and upper standard deviations,
e.g. `12 +3 -2 mBq/kg`.

Material assays often only give an upper limit. A limit is represented as a
value of zero with only an upper uncertainty, `0 +s -0`. Limits combine so that:

- a sum of limits is still a limit;
- adding a limit to a measured value doesn't raise the measurement's lower
  bound.

The uncertainties propagate as if they were gaussian. This is a practical
convention for building a background budget, not a strict statistical model:
results that include limits should be read as conservative estimates.

In plots, measured values are shown with 1σ error bars, and limits at their
90% upper limit. In spectra, bins that are upper limits are shaded up to their
one-sided 1σ (84.13%) upper limit.

## What is correlated

Independent random variables are the values that were entered or measured:

- the rates of the emission sources entered in a spec, e.g. an assay's U-238;
- each scalar and spectrum of each hit efficiency;
- each isotope's activation rate in an activated material;
- the radon level of each period of a radon exposure.

Everything else is calculated from these by sums and products, and keeps track
of which inputs it depends on. Uncertainties from a shared input add linearly
rather than in quadrature. For example, these are correctly treated as
correlated:

- one assay used by several components, or one component placed several times
  in an assembly;
- one hit efficiency used by several sources or components;
- activations of the same material, which share its activation rates, even
  with different exposure histories;
- sources generated from another source by the version settings, e.g. Ra-226
  in equilibrium with U-238.

This matters most for totals: the uncertainty of a sum of many components
using the same assay is much larger than adding them independently would
suggest. The results dashboard does every sum on the server so that filtered
totals keep these correlations.

Only sums and products are tracked. Division by an uncertain value and other
nonlinear operations treat their inputs as independent (and log a warning), as
do a few array operations such as slicing or integrating a spectrum.

## Storing correlated values

Each input has a permanent id, stored with its value, so the same hit
efficiency loaded in two calculations is the same variable. Values can't be
changed once made: editing a value makes a new variable, and everything that
depended on the old one is recalculated.

Calculated values are stored with their *expression*: the terms they're made
of, by the ids of their inputs. A stored result loaded later is therefore still
correlated with everything else that shares its inputs.

Expressions are much bigger than the values, so results that are only
displayed, like spectrum plots, are loaded without them. Such values can be
shown and scaled (e.g. to change units), but combining them with other
uncertain values raises an error rather than wrongly treating them as
independent.

{: .note }
Each stored expression repeats the inputs it uses, so the largest stored
results grow with the number of hit efficiency spectra in the model. Very large
models may approach MongoDB's 16 MB document limit. A shared store for the
inputs is planned.
