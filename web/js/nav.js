// Navigation hooks, filled in by main.js, so views can link to each other without import cycles.
export const nav = {
  go(mode) { void mode; },
  openMission(id, tab) { void id; void tab; },
  watchMission(id) { void id; },
  openCrew(tab) { void tab; },
  openPalette() {},
};
