---
title: Home
nav_order: 1
---

# Background Explorer

Background Explorer is a web application for building and tracking the
**radioactive background budget** of a radiation detector, such as a dark
matter or neutrinoless double beta decay experiment or an HPGe counter.

To build a background model, we answer two questions:
1. How much radioactivity is present at any given location relative to the
   detector, shielding, etc.?
2. What fraction of the radioactive emissions from that location reach the
   detector and interact?

Question 1 is answered by tracking all of the **components** used to build the
detector, housing, readout, shielding, structure, etc. Each component has a level
of **radioactive emissions** based on its material (measured by assay), mass,
surface area, and history of exposure to radon and cosmogenic activation.
Question 2 is most often addressed by Monte Carlo simulation to determine the
**hit efficiencies**.

A single **source term** of the background model is one
radioactive isotope, mapped to one simulated hit efficiency, normalized by
an assay and component mass. (Or radon exposure history and component surface
area, etc.) Background Explorer sums each contribution and related correlated
uncertainties, and presents the relative contributions to the total split by
component, material, isotope, and source category (assay, radon, activation, etc).



![The budget plots for the example model](assets/images/budget.png)

## Features

- **Uncertainty tracking**: asymmetric errors and upper limits, with
  correlations tracked between everything that shares an assay or a
  simulation.
- **Interactive results**: filter and drill down through the budget by
  component, isotope, material and category, with linked tables and spectra.
- **Version control**: branches and read-only tags of the whole model, with
  comparisons, single-document imports, merges, and exports to files.
- **Assay records**: keep sample, measurement and publication details with
  each assay, and import assays from [radiopurity.org](https://radiopurity.org).
- **Component tracking**: Each component has fields for recording vendor,
  purchase information, etc., as well as a table for tracking moves,
  modifications, exposure, etc. Components and Assays can both have attachments.
- **Calculated sources**: radon plate-out and cosmogenic activation from
  exposure histories; generated sources such as Ra-226 in equilibrium with
  U-238.
- **Users and roles**: anonymous or logged-in viewing, editors, admins.
- **A JSON API** and command line tools.

## What Background Explorer is NOT:
- An analysis framework. A complete background model will have hundreds of source terms.
  Combining all that data into a small enough number of templates to perform a
  likelihood or exclusion analysis is beyond bgexplorer's scope. Moreover,
  the asymmetric uncertainty calculations Background Explorer performs are
  designed to give intuitive results over statistical rigor.
- A simulation framework. Background Explorer can be used to generate a list
  of source terms without matching hit efficiencies in the data, but by design
  knows nothing about the simulation framework itself, nor about the data format.
  That's why results must be converted to the specified HitEfficiency format.
  Background Explorer also knows nothing about the detector response function
  (or even what kind of detector). This information can easily be encoded
  in the provided hit efficiencies; e.g., different spectra for 'raw',
  'fiducial volume', '2% energy resolution', etc.
- Currently (as of 3.0a1) Background Explorer does not have a planning tool.
  You cannot ask "what is the maximum U/Th/K level for compomnent X to stay
  below Y cpd/kg/keV?", nor can you do something like allcoating fractions of
  the available budget to specific components.

## Where to start

- [Getting started](getting-started.html): run a server and load an example.
- [Concepts](concepts.html): how components, sources and hit efficiencies fit
  together.
- [Building a model](model.html): every kind of document and its fields.
- [Results](results.html): reading the results pages.

{: .warning }
Background Explorer 3 is in **alpha**. The database layout may still change
between releases without migrations; export important versions to files.

## History

- **Version 1** was developed for the SuperCDMS SNOLAB experiment. The whole
  model was readonly, defined in Python code, tracked in git, and the database only
  cached calculations.
- **Version 2** was a general-purpose tool, but hit efficiencies were tracked
  separately from the rest of the model, so proper version control was impossible.
- **Version 3** stores every document in the database with in-database
  version control, and is configured entirely from the web interface.

Background Explorer is developed at [PNNL](https://www.pnnl.gov).
