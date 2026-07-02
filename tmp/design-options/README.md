# GLP-1 Site Design Options

This folder contains standalone static HTML mockups for the GLP-1 Reddit reports site. They are placeholder design references only; they are not wired into the live site build.

## Current Pick

**Option 02 - Drug Comparison Observatory** is the current preferred direction.

Why it fits:

- It makes the three public drug names the primary navigation model: Retatrutide, Tirzepatide, and Semaglutide.
- It avoids visible shorthand labels such as reta, tirz, and sema.
- It gives visitors a simple choice: compare drugs first, then drill into weight change, side effects, and methods.
- It is more polished than the current site without making a health-data project feel like a marketing page.

Open it directly:

```text
tmp/design-options/option-02-comparison-observatory.html
```

Or open the option index:

```text
tmp/design-options/index.html
```

## Files

- `index.html`: landing page for the six design previews, with Option 02 marked as the current pick.
- `option-01-clinical-dashboard.html`: quiet clinical dashboard.
- `option-02-comparison-observatory.html`: selected comparison-first design.
- `option-03-scientific-atlas.html`: sidebar atlas and research-tool layout.
- `option-04-public-signal-monitor.html`: live pipeline/status monitor layout.
- `option-05-academic-paper.html`: minimal academic paper style.
- `option-06-modern-data-product.html`: polished app-like data product.

## Navigation Direction

Use these labels in the real site:

- Overview
- Compare Drugs
- Weight Change
- Side Effects
- Methods
- Data Status
- Retatrutide
- Tirzepatide
- Semaglutide

Keep brand names as secondary explanatory copy:

- Tirzepatide: Mounjaro, Zepbound
- Semaglutide: Ozempic, Wegovy, Rybelsus

The existing URL paths can remain short for compatibility, for example `/tirz/`, but visible page labels should use full names.

## Porting Option 02 Later

To switch the generated site toward Option 02, update the site generator and shared assets rather than copying this mockup directly.

Likely files to edit:

- `scripts/build_site.py`: generated page structure, nav labels, page headings, summary cards, and drug-specific page text.
- `static/app.js`: any chart interaction labels, tooltip labels, and visible drug names.
- generated CSS inside `scripts/build_site.py` or any extracted stylesheet if the project later moves CSS into `static/`.

Implementation notes:

- Keep the static JSON/data pipeline unchanged.
- Preserve existing URLs unless there is a deliberate redirect plan.
- Replace visible shorthand labels with full names.
- Keep Reddit fitted curves separate from optional RCT overlays.
- Preserve the original-post hover/click detail behavior.
