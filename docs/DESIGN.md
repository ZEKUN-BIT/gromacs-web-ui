---
name: GROMACS 控制台
description: Local molecular dynamics runbench for guided GROMACS workflows
colors:
  paper: "#edf3ef"
  ink: "#18211e"
  muted: "#63716a"
  line: "#c9d8d0"
  panel: "#fffef8"
  field: "#f4f8f2"
  accent-lime: "#b7e957"
  accent-lime-hover: "#cdf46f"
  success-green: "#327a54"
  success-deep: "#2c5e33"
  success-toast: "#274d2c"
  success-soft-text: "#d9f2c2"
  info-teal: "#256f78"
  info-deep: "#174f57"
  info-soft: "#dbeeea"
  warning-amber: "#b86f1c"
  warning-deep: "#7a5200"
  warning-soft: "#fdf3dc"
  danger-red: "#b95449"
  danger-soft: "#fde5dc"
  danger-soft-text: "#ffd9d2"
  disabled: "#6e756e"
  cancelled-bg: "#e6e3da"
  cancelled-text: "#62685c"
  code-text: "#333333"
  terminal-bg: "#10120f"
  terminal-text: "#d8efbb"
typography:
  display:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, PingFang SC, sans-serif"
    fontSize: "34px"
    fontWeight: 800
    lineHeight: 1.12
    letterSpacing: "0"
  body:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  panel-title:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, PingFang SC, sans-serif"
    fontSize: "17px"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "0"
  brand-title:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, PingFang SC, sans-serif"
    fontSize: "21px"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "0"
  mobile-display:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, PingFang SC, sans-serif"
    fontSize: "28px"
    fontWeight: 800
    lineHeight: 1.12
    letterSpacing: "0"
  button:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, sans-serif"
    fontSize: "15px"
    fontWeight: 750
    lineHeight: 1.2
    letterSpacing: "0"
  small-body:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0"
  label:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, sans-serif"
    fontSize: "12px"
    fontWeight: 750
    lineHeight: 1.25
    letterSpacing: "0"
  caption:
    fontFamily: "IBM Plex Sans, Segoe UI, HarmonyOS Sans SC, MiSans, Microsoft YaHei UI, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, sans-serif"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0"
  micro:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 900
    lineHeight: 1.2
    letterSpacing: "0"
  mark:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "16px"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "0"
  mono:
    fontFamily: "JetBrains Mono, Cascadia Mono, IBM Plex Mono, SFMono-Regular, Consolas, monospace"
rounded:
  sm: "7px"
  md: "10px"
  lg: "12px"
  pill: "999px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  xl: "22px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.accent-lime}"
    rounded: "{rounded.sm}"
    padding: "0 24px"
    height: "46px"
  field:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "9px 11px"
    height: "40px"
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

The visual system uses a dark local-control rail, light work surfaces, restrained borders, and one high-visibility accent for primary action and current selection. Scientific terminology remains visible and practical; guidance is delivered through readiness states, command preview, and run checks rather than promotional copy.

**Key Characteristics:**

- Dense but readable form controls.
- Visible execution state and command transparency.
- Chinese-first labels with GROMACS terms preserved.
- Accent color used for selection and readiness, not decoration.

## Colors

The palette is cool technical paper with dark instrumentation and a single lime action accent.

### Primary

- **Instrument Ink**: Main text, sidebar base, high-confidence outlines, terminal shell.
- **Runbench Lime**: Current workflow, primary action text, selection feedback, and successful readiness emphasis.

### Secondary

- **Procedure Teal**: Health, focus, section labels, and informational state.

### Tertiary

- **Warning Amber**: Missing inputs and blocked preview state.
- **Danger Red**: Failed, cancelled, or destructive task actions.

### Neutral

- **Cool Paper**: Page background.
- **Panel White**: Main containers and control surfaces.
- **Field Green-Gray**: Inputs, metric cells, inactive controls.
- **Line Gray**: Quiet dividers and inactive borders.

### Named Rules

**The Accent Rarity Rule.** Lime appears only on primary actions, selected workflows, and satisfied readiness states. It should never become a background theme.

## Typography

**Display Font:** IBM Plex Sans / Segoe UI / HarmonyOS Sans SC  
**Body Font:** IBM Plex Sans / Segoe UI / HarmonyOS Sans SC / Source Han Sans SC  
**Label/Mono Font:** JetBrains Mono / Cascadia Mono / IBM Plex Mono / Consolas

**Character:** Product typography, not brand typography. One sans stack carries most UI, while mono is reserved for command-like labels, values, paths, and compact status readouts.

### Hierarchy

- **Display** (800-900, 34px, 1.12): Screen title only.
- **Mobile Display** (800-900, 28px, 1.12): Screen title below mobile breakpoints.
- **Brand Title** (800, 21px, 1.1): App identity in the rail.
- **Panel Title** (800-900, 17px, 1.2): Panel and workflow guide headings.
- **Button** (750-800, 15px, 1.2): Primary action labels.
- **Small Body** (700, 13px, 1.4): Compact cards, job list names, and dense helper rows.
- **Body** (400-700, 12-14px, 1.4-1.5): Guidance, helper text, file summaries, and task details.
- **Label** (750-800, 12px, 1.25): Form labels, buttons, tabs, status names.
- **Caption** (700, 11px, 1.3): Secondary labels and compact metadata.
- **Micro** (900, 10px, 1.2): Tiny machine-facing status inside chips only.

### Named Rules

**The Mono Evidence Rule.** Use mono only when the content is a command, path, parameter code, numeric status, or machine-facing label.

## Layout

The app uses a persistent left rail and a right-side workbench. Desktop keeps the setup form and parameter bank side by side; mobile collapses into a single task-first stack. The primary reading order is: choose workflow, satisfy inputs, inspect commands, run job, read logs, download files.

Form groups use CSS Grid with stable minimum widths so long Chinese labels and scientific terms wrap inside their cells without changing neighboring controls. Monitor panels use a left stack for command preview and outputs, plus a right job panel for status, progress, and logs.

## Elevation & Depth

Depth is structural and restrained. Panels use 1px borders, light tonal fills, and short offset shadows only for major containers. Hover states may lift slightly, but the surface should never feel like a pile of floating cards.

### Shadow Vocabulary

- **Workbench Shadow**: Soft ambient shadow for large panels.
- **Control Shadow**: Small state shadow for buttons and selected items.

## Shapes

The shape system is compact and consistent. Panels use a 10px corner, controls use 7px, and pills are limited to small counters or section tags. Inputs, buttons, workflow tabs, chips, and panels should not mix unrelated radius styles.

## Components

### Buttons

- **Shape:** Compact rounded rectangle (7px).
- **Primary:** Dark background with lime text, 46px minimum height.
- **Disabled:** Muted dark-gray background with lime-soft text and a clear "补全输入" label.
- **Hover / Focus:** Small movement or focus ring, never animated choreography.

### Chips

- **Style:** Small rectangular chips with concise status text.
- **State:** Missing inputs use amber text; satisfied inputs use green and lime-soft fill.

### Cards / Containers

- **Corner Style:** 10px.
- **Background:** Panel White or Field Green-Gray.
- **Shadow Strategy:** Use shadows only on major page-level surfaces.
- **Border:** 1px black for structural shells, 1px line gray for internal grouping.
- **Internal Padding:** 14-20px depending on density.

### Inputs / Fields

- **Style:** Label above input, 40px minimum height, 7px radius, solid border.
- **Focus:** Teal or lime focus ring with no layout shift.
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
- **Do** keep mobile controls at least 44px tall when they are tappable.

### Don't:

- **Don't** turn the page into a landing page or hero composition.
- **Don't** hide command details behind decorative summaries.
- **Don't** use wide decorative color rails on cards or alerts.
- **Don't** add fake scientific claims, benchmark numbers, customers, or validation promises.
- **Don't** introduce heavy animation that delays repeated task work.
