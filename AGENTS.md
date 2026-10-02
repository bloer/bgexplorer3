# Background Explorer version 3
## Overview
This is a web application intended to be used as a tool to model backgrounds
in radiation detectors. Each entry in the background model comes from one
Component, one associated EmissionSource (usually a material assay measurement),
and one associated HitEfficiency (usually derived from a Monte Carlo simulation).
A model is the total combination of many of each class. Models feature a
git-like branching and tagging version tracking system. Versions can be
compared, single documents imported between them, and whole versions merged
with one rule for conflicts (see README "Versions"). Summary views for each version can be configured
to show/hide different columns, use different notes, etc.

## Structure
- MongoEngine ORM, with most classes inheriting from VersionedDocument to allow in-DB version control.
- Flask
- Docker (not yet implemented)
- numpy/matplotlib for histogram calculations and plotting
- AsymmetricUncertainty based on python uncertainties package, modified to handle upper limits (common from material assays) in a non-statistical way.
- strict enforcement of units with pint


## History
- Version 1 developed for the SuperCDMS SNOLAB experiment. The whole model is defined
purely in python code, tracked in git. The database backing was introduced only to cache the output of calculations.
- Version 2 intended to be a general purpose tool. No requirements on HitEfficiency format, so users need to provide functions to evaluate their data. Models are saved as a single document, but HitEfficiencies are a regular collection, so proper version control was impossible.
- Version 3 move to an ORM, collections are treated in a more standard way. Focus on maintaining version control and everything needed to configure the server given by the database.