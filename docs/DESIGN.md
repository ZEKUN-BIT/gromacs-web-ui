---
name: GROMACS 控制台
description: Local molecular dynamics runbench for guided GROMACS workflows
# Runtime values are defined in app/static/design-tokens.css.
colors:
  paper: "#f5f7fa"
  panel: "#ffffff"
  field: "#f7f9fb"
  rail: "#f0f3f6"
  ink: "#202b3b"
  muted: "#55657a"
  line: "#e2e8f0"
  action-teal: "#0b786f"
  on-action: "#ffffff"
  success-green: "#237951"
  success-soft: "#edf7f1"
  info-soft: "#eaf5f2"
  warning-amber: "#936316"
  danger-red: "#b43e3e"
  danger-soft: "#fff0ee"
typography:
  display:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0"
  body:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: "0"
  chat-body:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.75
    letterSpacing: "0"
  panel-title:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0"
  brand-title:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0"
  result-dialog-title:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0"
  mobile-display:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0"
  button:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "0"
  small-body:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0"
  label:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "0"
  caption:
    fontFamily: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  micro:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "0"
  mark:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0"
  mono:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
rounded:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "14px"
spacing:
  2xs: "4px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.action-teal}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: "0 24px"
    minHeight: "42px"
  field:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "9px 11px"
    minHeight: "40px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "16px"

---

# Design System: GROMACS 控制台

## Overview

**Creative North Star: "Local Runbench"**

This interface is an operating surface for scientific work. It should feel like a reliable local instrument panel: direct, readable, dense where useful, and explicit about state. The design should not compete with the simulation task.

The visual system uses a neutral task rail, distinct work surfaces, restrained borders, and a teal accent for primary actions and current selection. The palette follows the system light/dark preference. Scientific terminology remains visible and practical; guidance is delivered through readiness states, command preview, and run checks rather than promotional copy.

**Key Characteristics:**

- Dense but readable form controls.
- Visible execution state and command transparency.
- Chinese-first labels with GROMACS terms preserved.
- Accent color used for selection and readiness, not decoration.

## Source of Truth

`app/static/design-tokens.css` owns font loading, the light/dark palette, typography scale, spacing, radii, and shared shadows. The stylesheet order is `design-tokens.css` → `styles.css` → `icons.css` → `desktop.css` → `product.css` → `agent.css` → `workflow-feedback.css`. Theme tokens must not be redefined in component stylesheets.

`styles.css` owns common fields, buttons, focus treatment, and workspace/detail/research navigation. `desktop.css` contains workstation and scientific component layout. `product.css` contains onboarding, organization, dialogs, and viewport-specific adjustments. `agent.css` contains AI workspace components; `workflow-feedback.css` contains connection and validation feedback. Extend the existing component or its named variant instead of appending another global override. Keep responsive differences in their media rules.

## Colors

The palette uses cool neutral work surfaces with teal actions and semantic green, amber, and red status colors.

| Token | Light | Dark | Purpose |
| --- | --- | --- | --- |
| `--paper` | `#f5f7fa` | `#151c24` | Workspace background |
| `--panel` | `#ffffff` | `#1c2530` | Panels and fields |
| `--field` | `#f7f9fb` | `#222e3b` | Secondary surfaces |
| `--rail-background` | `#f0f3f6` | `#19222c` | Task navigation |
| `--ink` | `#202b3b` | `#e6edf5` | Primary text |
| `--muted` | `#55657a` | `#a3b2c4` | Secondary text |
| `--blue` | `#0b786f` | `#85d6c6` | Actions, links, and focus |
| `--on-accent` | `#ffffff` | `#172c31` | Text on primary actions |
| `--green` | `#237951` | `#8bd4b0` | Success |
| `--amber` | `#936316` | `#e4ba73` | Warnings |
| `--red` | `#b43e3e` | `#ee9b96` | Errors and destructive actions |

Secondary text must reach at least 4.5:1 against its actual background, including sidebar, selected, hover, and focus states. `--muted` has margin above that threshold on the light neutral surfaces; changing a background still requires measuring the resulting pair. Disabled controls and decorative icons are evaluated separately from ordinary text. Focus outlines must remain visible and statuses must also use text or symbols.

Scientific `--series-*` colors are independent from UI text tokens. Preserve series identity and redundant line/marker encodings; do not recolor scientific traces to fix navigation or helper-text contrast.

## Typography

**Display, Body, and Label Font:** Inter with platform Chinese sans-serif fallbacks.

**Code/Mono Font:** JetBrains Mono / Cascadia Mono / IBM Plex Mono / Consolas

The shared UI stack is `Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif`. Inter is self-hosted in `app/static/fonts/inter-variable.woff2`; its SIL Open Font License is included as `Inter-OFL.txt`. Chinese glyphs use an available system font from the stack. Screenshots and browser font checks should verify the actual CJK fallback on each supported platform.

**Character:** Product typography, not brand typography. One sans stack carries UI text and ordinary parameter inputs, including numeric parameters. Mono is reserved for code, commands, logs, paths, and numeric statistics in tables.

### Hierarchy

- **Page Title** (600, 24px, 1.4): Screen title on desktop.
- **Mobile Page Title** (600, 22px, 1.4): Screen title on narrow layouts.
- **Panel and Brand Title** (600, 16px, 1.5): Desktop panel, form column, and app identity headings.
- **Result and Dialog Title** (600, 18px, 1.5): Larger result and dialog headings.
- **UI Body** (400, 14px, 1.65): Task details, file summaries, and ordinary parameter values.
- **Labels and Buttons** (500, 14px, 1.5): Form labels, navigation, tabs, and action labels.
- **Explanations** (400, 13px, 1.6): Guidance and helper text.
- **Metadata** (400-500, 12px, 1.5): Timestamps, secondary labels, and compact readouts.
- **Chat Body** (400, 16px, 1.75): User and assistant messages.

Use only 400, 500, and 600 for the UI hierarchy. Keep Chinese text at least 12px. Mobile labels and buttons remain 14px, while input text uses 16px. Reduce columns or collapse secondary content instead of shrinking text to 10-11px.

### Named Rules

**The Mono Evidence Rule.** Use mono for code, commands, logs, paths, and numeric statistics in tables. Ordinary parameter inputs, including numbers, use the shared UI font.

## Layout

The app uses a persistent left rail and a right-side workbench. Desktop keeps the setup form and parameter bank side by side; mobile collapses into a single task-first stack. The primary reading order is: choose workflow, satisfy inputs, inspect commands, run job, read logs, download files.

Form groups use CSS Grid with stable minimum widths so long Chinese labels and scientific terms wrap inside their cells without changing neighboring controls. Monitor panels use a left stack for command preview and outputs, plus a right job panel for status, progress, and logs.

## Elevation & Depth

Depth is structural and restrained. Panels use 1px borders, light tonal fills, and soft shadows only where needed. Common buttons and navigation change color without moving on hover.

### Shadow Vocabulary

- **Workbench Shadow**: Soft ambient shadow for large panels.
- **Control Shadow**: Small state shadow for buttons and selected items.

## Shapes

The shape system is compact and consistent. Shared controls use the 8px radius token; standard panels use 12px and large workstation containers use 14px. Small tabs may use the 4px token. Circular help controls, status dots, and asymmetric chat bubbles retain their intentional shapes.

## Components

### Buttons

- **Shape:** Compact rounded rectangle (8px).
- **Primary:** Teal background with theme-aware contrasting text, 42px minimum height.
- **Disabled:** Secondary surface, muted text, and a clear "补全输入" label when required inputs are missing.
- **Hover / Focus:** Color change and a 2px visible focus outline with 3px offset; no movement.

### Chips

- **Style:** Small rectangular chips with concise status text.
- **State:** Missing inputs use amber text; satisfied inputs use green and a pale semantic fill.

### Cards / Containers

- **Corner Style:** 12px, or 14px for the main workstation containers.
- **Background:** Theme-aware panel or secondary surface.
- **Shadow Strategy:** Use shadows only on major page-level surfaces.
- **Border:** 1px theme-aware line color.
- **Internal Padding:** 14-20px depending on density.

### Inputs / Fields

- **Style:** Label above input, 40px minimum height, 8px radius, solid border.
- **Focus:** Teal outline and soft focus ring with no layout shift.
- **Error / Disabled:** Error text must name the recovery action, not only the failure.

### Navigation

The left rail owns system status, GROMACS binary configuration, and job navigation. It stays compact and should not become a marketing sidebar.

### Workflow Guide

The guide combines current workflow name, readiness count, progress bar, ordered steps, missing input chips, and a next-action sentence. It is the main onboarding pattern and must update with the selected workflow.

## Do's and Don'ts

### Do:

- **Do** keep generated GROMACS commands visible before submission.
- **Do** make missing inputs visible in the workflow guide and command preview.
- **Do** preserve form field names when changing UI layout.
- **Do** use semantic state colors for success, warning, danger, and info.
- **Do** aim for 44px touch targets; retain adequate target spacing for compact controls and verify the 24px WCAG 2.2 minimum or its spacing exception.

### Don't:

- **Don't** turn the page into a landing page or hero composition.
- **Don't** hide command details behind decorative summaries.
- **Don't** use wide decorative color rails on cards or alerts.
- **Don't** add fake scientific claims, benchmark numbers, customers, or validation promises.
- **Don't** introduce heavy animation that delays repeated task work.
