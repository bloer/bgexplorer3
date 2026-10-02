---
title: Home
nav_order: 1
---

# Background Explorer

Background Explorer is a web application for building and tracking the
**radioactive background model** of a radiation detector, such as a dark
matter or neutrinoless double beta decay experiment, or a superconducting
sensor.

A background model combines:

- the detector's **components**: what they are made of, how big they are, and
  how they are assembled;
- their **radioactivity**: material assays, estimates, radon exposure and
  cosmogenic activation;
- the detector's **response** to each source in each place, usually from Monte
  Carlo simulations.

Background Explorer multiplies these out for every component, source and
place, sums them, and shows the result: background rates, counts in regions of
interest and energy spectra, broken down by component, isotope, material and
source category.

![The budget plots for the example model](assets/images/budget.png)

## Features

- **Uncertainties done right**: asymmetric errors and upper limits, with
  correlations tracked between everything that shares an assay or a
  simulation.
- **Interactive results**: filter and drill down through the budget by
  component, isotope, material and category, with linked tables and spectra.
- **Version control**: branches and read-only tags of the whole model, with
  comparisons, single-document imports, merges, and exports to files.
- **Assay records**: keep sample, measurement and publication details with
  each assay, and import assays from [radiopurity.org](https://radiopurity.org).
- **Calculated sources**: radon plate-out and cosmogenic activation from
  exposure histories; generated sources such as Ra-226 in equilibrium with
  U-238.
- **Users and roles**: anonymous or logged-in viewing, editors, admins.
- **A JSON API** and command line tools.

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
  model was defined in Python code, tracked in git, and the database only
  cached calculations.
- **Version 2** was a general-purpose tool, but models were saved as single
  documents, so proper version control wasn't possible.
- **Version 3** stores every document in the database with in-database
  version control, and is configured entirely from the web interface.

Background Explorer is developed at [PNNL](https://www.pnnl.gov).
