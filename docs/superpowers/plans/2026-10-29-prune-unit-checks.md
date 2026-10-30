# Prune Unit Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Check units only when EmissionSources, Components and
HitEfficiencies are saved. Match SourceTerms only to HitEfficiencies whose
norm fits the source's multiplier. Remove the SourceTerm-level checks.

**Architecture:**
- `Multiplier.norm` is the single mapping from source multiplier to
  HitEfficiency norm. `SourceTerm.hiteffs_query` and `signals.update_hiteff`
  both filter on it.
- The rate × multiplier × value product then always fits the display unit,
  so `TermCalculator` only converts. Its existing `UnitMismatchError` stays
  as the backstop for data written without validation.
- `check_version_units` becomes a per-document audit, used after imports and
  merges.

**Tech Stack:** mongoengine, pint (`bgexplorer.models.common.units`), unittest
run with pytest.

Spec: `docs/superpowers/specs/2026-10-29-prune-unit-checks-design.md`

## Global Constraints

- Use `.venv/bin/python` in this repo only.
- Alpha: no migrations or API compatibility. Stored `norm: 'none'` and
  `multiplier: 'none'` simply stop loading.
- Keep `examples/qis/qis.py` working.
- Commit after each task; don't push.
- Baseline before the last commit: 17 failing tests, among them
  `test_app.py::TestAppVersions::test_readonly`, several in
  `test_app_components.py` and `test_auth.py::TestEnteredBy::test_enteredby`.
  Don't count those as regressions.
- Test command: `.venv/bin/python -m pytest -q -p no:warnings --ignore=tests/test_browser.py`

---

### Task 1: EmissionSource units

**Files:**
- Modify: `src/bgexplorer/models/isotope.py` (add `NATURAL_K`, `is_radioactive`)
- Modify: `src/bgexplorer/models/emissionspec.py` (`Multiplier`,
  `EmissionSource`, `EmissionSpec.clean`)
- Modify: `src/bgexplorer/models/radiopurity.py:337`
- Modify: `tests/test_emissionspec.py` (removed `Multiplier.none`, flux
  multipliers, spec multiplier fills only blanks)
- Rewrite: `tests/test_unitcheck.py` (clean tests; Tasks 2–3 add more)

**Produces:**
- `isotope.is_radioactive(name) -> bool`
- `Multiplier.valid_units -> pint.Unit`
- `Multiplier.get_multiplier(rate, allow_mass_to_rate=False)`
- `Multiplier.check_units(rate, allow_mass_to_rate=True)`
- `Multiplier.accepts(rate, allow_mass_to_rate=True)`
- `EmissionSource.converts_mass -> bool`
- `EmissionSource.standardized_rate`

- [ ] **Step 1: Write the failing tests.** Replace `tests/test_unitcheck.py`
  with classes `TestRejectedAtClean` and `TestAcceptedAtClean`, both based on
  `AppTestCase` with `vc.create_version('main')` in `setUp`.
  - Rejected (each a subTest; assert `ValidationError` and that nothing was
    saved):
    - `Bq/kg` + surface, `Bq` + mass, `Bq/m**3` + length, `1/cm**2/s` + mass,
      `ppb` + flux, each on U238
    - `1/s/kg**2` and `m` with no multiplier
    - `ppb`, `g/m**2` and `ug` on `Gammaflux` and `Pb206`, matching
      'radioactive isotope'
    - a Component mass of `2 m`
    - a HitEfficiency with `v1='1 kg'` after one with `RATE`
    - a flux-norm HitEfficiency with `RATE` scalars ('has incorrect units')
  - Accepted:
    - U238 `10 ppb`, `1e-9` → mass; `1 ug/m**2` → surface; `1 ug/m**3` →
      volume; `1 ug` → per_piece; K `0.4 ppb` → mass. `standardized_rate`
      equals `concentration_to_rate(name, rate)` and has the dimensions of
      `multiplier.valid_units`.
    - A rate of None saves, with a None multiplier.
    - `7 1/cm**2/s` defaults to surface.
    - Explicit flux and flux_per_sr save.

- [ ] **Step 2: Run.** `.venv/bin/python -m pytest -q -p no:warnings tests/test_unitcheck.py`
  Expected: failures (`'str' object has no attribute 'check'`).

- [ ] **Step 3: isotope.py.** Add `NATURAL_K = ('K', 'natK', 'K-nat', 'Knat')`
  and use it in both converters:

```python
def is_radioactive(source: IsotopeType) -> bool:
    """ Can a quantity of `source` be converted to a decay rate? """
    if source in NATURAL_K:
        source = 'K40'
    return get_halflife(source) is not None
```

- [ ] **Step 4: Multiplier.** Make `valid_units` a dict of parsed
  `units.Unit`s built once. Flux is `1/cm**2/s` and flux_per_sr
  `1/cm**2/s/sr`. Then:

```python
    @classmethod
    def get_multiplier(cls, rate, allow_mass_to_rate=False):
        """ For rate specified in given units, find the appropriate
        multiplier, also for a mass of an isotope if `allow_mass_to_rate`.
        Raise ValidationError if multiplier can't be determined. Fluxes have
        the dimensions of surface activities, so are never chosen
        """
        if rate is None:
            return None
        for allow in (False, allow_mass_to_rate):
            for mult in (cls.mass, cls.volume, cls.surface, cls.length,
                         cls.per_piece):
                if mult.accepts(rate, allow):
                    return mult
        raise ValidationError(f"Unhandled rate units {rate.u}")

    def check_units(self, rate, allow_mass_to_rate=True) -> None:
        if rate is None:
            return
        dims = self.valid_units.dimensionality
        if rate.u.dimensionality == dims:
            return
        if (allow_mass_to_rate and
                (rate.u * _MASS_TO_RATE).dimensionality == dims):
            return
        raise ValidationError(f"Rate units {rate.u} invalid"
                              f" for multiplier {self.name}")
```

- [ ] **Step 5: EmissionSource.**
  - `converts_mass` returns `is_radioactive(self.name)`. It replaces
    `is_isotope` and `get_isotope`.
  - `standardized_rate` returns `self.rate` if
    `multiplier.accepts(rate, False)`, otherwise
    `concentration_to_rate(self.name, self.rate)`.
  - `clean`:

```python
    def clean(self):
        """ Choose the multiplier from the rate's units if it isn't set, and
        make sure the rate fits it. A radioactive isotope's rate may be given
        as a mass of it, e.g. a concentration
        """
        if self.rate is None:
            return
        converts = self.converts_mass
        try:
            if self.multiplier is None:
                self.multiplier = Multiplier.get_multiplier(self.rate,
                                                            converts)
            self.multiplier.check_units(self.rate, converts)
        except ValidationError as e:
            candidates = [self.multiplier] if self.multiplier else Multiplier
            if not converts and any(m.accepts(self.rate)
                                    for m in candidates):
                raise ValidationError(
                    "to give the rate as a mass, e.g. a concentration, the "
                    "name must be a radioactive isotope",
                    field_name='name') from e
            # the rate is what's wrong, so forms can show it there
            raise ValidationError(e.message, field_name='rate') from e
```

- [ ] **Step 6: EmissionSpec.clean and radiopurity.**
  - Move `source.category = source.category or self.category` out of the
    `if self.multiplier` block.
  - Change the multiplier `help_text` to "Used for every source without its
    own".
  - In radiopurity, call `Multiplier.get_multiplier(..., allow_mass_to_rate=True)`.

- [ ] **Step 7: Update `tests/test_emissionspec.py`.**
  - test1: `1/s` → per_piece.
  - test2_checkunits:
    - `Bq/m**2` is accepted by surface, inner, outer, flux and flux_per_sr.
    - `Bq` is accepted only by per_piece.
  - test2_getvalue: per_piece, flux and flux_per_sr give 1.
  - test2_spec_multiplier:
    - the spec's multiplier fills in only sources without one
    - a source without one that doesn't fit is an error
    - sources keep their stored multiplier

- [ ] **Step 8: Run.** `tests/test_unitcheck.py tests/test_emissionspec.py tests/test_radiopurity.py`
  all pass.

- [ ] **Step 9: Commit.** "Check EmissionSource units once, allowing
  isotope masses".

### Task 2: Match HitEfficiencies by norm

**Files:**
- Modify: `src/bgexplorer/models/hiteff.py` (`NormMultiplier`: drop `none`
  and `check`)
- Modify: `src/bgexplorer/models/emissionspec.py` (add `Multiplier.norm`,
  `Multiplier.for_norm`)
- Modify: `src/bgexplorer/models/sourceterm.py` (`hiteffs_query`,
  `find_hiteffs`)
- Modify: `src/bgexplorer/models/signals.py` (`update_hiteff`)
- Test: `tests/test_unitcheck.py` (`TestHitEfficiencyMatching`)

**Consumes:** Task 1's `standardized_rate`.

**Produces:**
- `Multiplier.norm -> NormMultiplier`
- `Multiplier.for_norm(norm) -> list[Multiplier]`

- [ ] **Step 1: Write the failing tests.**
  - `setUp` saves three U238 HitEfficiencies at `c1`: rate/`RATE`,
    flux/`FLUX` (`dru*s*cm**2`) and flux_per_sr/`FLUX_SR`
    (`dru*s*cm**2*sr`).
  - Helper `term(rate, multiplier=None, location='c1', **component_kwargs)`
    saves a one-source spec and a component, and returns
    `SourceTerm.objects.get()`.
  - Each test asserts which norms the term matched and, where given, the
    `v1` value from `TermCalculator` compared with `(value × rate).to(display)`:
    - `10 mBq/kg`, 2 kg → {rate}, 2 dru
    - `10 ppb`, 2 kg → {rate}, using `concentration_to_rate`
    - `10 mBq/m**2` inner_surface, inner 3 m², outer 5 m² → {rate}, 3 dru
    - `2 1/cm**2/s` flux → {flux}, 0.2 dru
    - `2 1/cm**2/s/sr` flux_per_sr → {flux_per_sr}, 0.2 dru
    - `2 1/cm**2/s` with no multiplier → surface, {rate}
    - rate None → set()
  - At location c2, a flux HitEfficiency saved after the term is not attached,
    but a rate one is.
  - Changing the rate HitEfficiency's norm to flux, with `FLUX` scalars,
    detaches it.
  - Changing the source to flux, then to no rate, swaps the matches to
    {flux} and then to set().

- [ ] **Step 2: Run.** Expected failures: wrong matches, and the
  AttributeError `source_multiplier`.

- [ ] **Step 3: NormMultiplier.** Drop `none` and `check`; update the
  docstring.

- [ ] **Step 4: Multiplier.**

```python
    @property
    def norm(self) -> NormMultiplier:
        """ The norm of the HitEfficiencies for our rates, times our value
        """
        return {Multiplier.flux: NormMultiplier.flux,
                Multiplier.flux_per_sr: NormMultiplier.flux_per_sr,
                }.get(self, NormMultiplier.rate)

    @classmethod
    def for_norm(cls, norm) -> list:
        """ The multipliers whose rates HitEfficiencies with `norm` fit """
        return [m for m in cls if m.norm is norm]
```

- [ ] **Step 5: SourceTerm.**
  - `hiteffs_query`: return `.none()` without a multiplier, otherwise filter
    on `norm=self.source.multiplier.norm`.
  - `find_hiteffs`: replace when `replace and (hiteffs or
    self._data.get('hiteffs'))`.
  - Drop the alias imports.

- [ ] **Step 6: signals.update_hiteff.** Add
  `source__multiplier__in=Multiplier.for_norm(hiteff.norm)` to `matches`.

- [ ] **Step 7: Run** `tests/test_unitcheck.py tests/test_sourceterm.py tests/test_hiteff.py`.
  All pass.

- [ ] **Step 8: Commit.** "Match HitEfficiencies to SourceTerms by norm".

### Task 3: Remove SourceTerm-level checks; per-document audit

**Files:**
- Modify: `src/bgexplorer/models/sourceterm.py`
  - remove `check_units`, `unit_checks_deferred`, `_unit_checks_deferred`,
    `hiteff_rate_units`, `unweighted_rate`, and the norm check in
    `_calculate`
  - rewrite `check_version_units`
- Modify: `src/bgexplorer/models/hiteff.py` (remove `check_sourceterms`)
- Modify: `src/bgexplorer/models/termcalc.py`
  - remove `_check_norm`, `check`, `check_hiteff`, `strict`, `_norms`
  - `_rate` uses `source.standardized_rate`
- Modify: `src/bgexplorer/models/versionfile.py`,
  `src/bgexplorer/models/versioncontrol.py` (drop the deferral)
- Test: `tests/test_unitcheck.py` (`TestAudit`)

- [ ] **Step 1: Write the failing tests.**
  - A bad rate-norm HitEfficiency (`v1='1 kg'`) saved with `validate=False`
    is attached. `check_version_units` reports its id, and `dashboard`
    raises `UnitMismatchError`.
  - A spec saved with `validate=False` holding `Gammaflux` `1 ppb` is
    reported.
  - Display units are filled in after the settings are replaced.
  - The results page shows "Units in the saved data don't match", and the
    JSON message says "not the display unit".
  - `test_roi_in_terms` is kept as is.

- [ ] **Step 2: Run.** Expected: the source audit test fails.

- [ ] **Step 3: Rewrite `check_version_units(version)`.**

```python
def check_version_units(version: str) -> List[str]:
    """ Audit the units of everything in `version`, for data that was written
    without being validated, such as an import or a merge. HitEfficiencies'
    scalars, spectra and ROIs without a display unit in the settings are
    given one, and every HitEfficiency and EmissionSource is checked as
    saving it would be. Returns a description of each problem found, which
    is empty if the units all agree.
    """
    vsettings = settings.get_settings(version, create=False)
    config = vsettings.hiteffdbconfig
    before = config.to_mongo()
    problems = []
    for hiteff in HitEfficiency.select_version(version):
        config.update_from(hiteff)
        try:
            hiteff.check_dbconfig(config)
        except ValidationError as e:
            problems.append(f"HitEfficiency {hiteff.id} ({hiteff}): "
                            f"{e.message}")
    if config.to_mongo() != before:
        # update rather than save: the version may be locked or read-only
        vsettings.update(set__hiteffdbconfig=config)
        settings.touch(version)
    for spec in EmissionSpec.select_version(version):
        for source in spec.sources:
            try:
                source.clean()
            except ValidationError as e:
                problems.append(f"{spec.name}, source {source.name}: "
                                f"{e.message}")
    problems = list(dict.fromkeys(problems))
    for problem in problems:
        log.warning("Unit mismatch in version '%s': %s", version, problem)
    return problems
```

- [ ] **Step 4: Remove the listed code.** Update the docstrings of
  `TermCalculator` and `UnitMismatchError`. Remove imports that are no longer
  used (`ContextVar`, `contextmanager`, `TermCalculator` and
  `UnitMismatchError` in sourceterm; `UnitMismatchError` in hiteff).

- [ ] **Step 5: Run the full suite.** Compare against the baseline.

- [ ] **Step 6: Commit.** "Remove SourceTerm-level unit checks".

### Task 4: Docs and examples

- [ ] `docs/model.md`:
  - multiplier table: per piece, flux and flux per sr replace none
  - isotope-mass paragraph
  - flux must be set explicitly
  - HitEfficiencies are matched by norm
  - drop `none` from the Normalization row
- [ ] `docs/settings.md`: replace the paragraph on SourceTerm and
  HitEfficiency rejection with norm matching plus the audit.
- [ ] `examples/qis/qis.py:161`: `multiplier='flux'`.
- [ ] `examples/cdms/specs.py`:
  - per_piece → `Multiplier.per_piece`
  - flux → `Multiplier.flux`
  - `surface_in` / `surface_out` → `inner_surface` / `outer_surface`
  - `'bulk': 'none'` → `'per_piece'`
- [ ] `examples/cdms/geometry.py:113`: `multiplier='flux'` (environmental
  material fluxes).
- [ ] Run `populate_example` for qis against the test database. Check the
  Gammaflux term matches the flux HitEfficiency and results are printed.
- [ ] Commit "Update docs and examples for flux multipliers" (qis and docs;
  cdms is untracked).
