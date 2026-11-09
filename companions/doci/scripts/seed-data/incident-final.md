# 2026-05-12 — irrigation pump failure

> Incident postmortem. Field 3 main pump failed Sunday morning; strawberries in rows 1-12 took heat stress. Resolved 2026-05-14.

## What happened

Main irrigation pump kicked out around 03:00 Sunday morning. The pump's auto-restart contactor was already on its third trip-cycle of the season, but no alert was wired. Marina found the dry field on her 07:00 walk-through — by then field 3 had been without water for ~4 hours on a 32°C day.

## Damage (confirmed at Saturday harvest)

- Strawberry rows 1-12: ~18% pick loss for W19. The dip is visible on the [operations dashboard](/farm) harvest chart.
- Salad greens (field 7): unaffected — separate water line off the secondary pump.
- No livestock impact.

## Root cause

Two failures stacked:

1. **Contactor wear.** The pump's auto-restart logic was on its third trip-cycle of the season. We should have caught this on the monthly equipment check; the inspection sheet does not currently include the contactor.
2. **Alert path.** The pump has telemetry but it pushes to a Slack channel nobody watches over the weekend. The failure happened at 03:00 — by the time anyone read Slack, the field was dry for 4 hours.

## Action items

- [x] Pump replaced (Aresco, 2026-05-13)
- [x] Telemetry rerouted to the on-call SMS list
- [ ] Annual contactor replacement added to the equipment maintenance calendar
- [ ] Second sensor (soil moisture, field 3) — quote pending from Aresco

## Related

- [Operations dashboard](/farm) — W19 harvest dip is on the chart.
- [CSA share packing protocol](/farm/sop/csa-pack) — affected members were notified during Saturday pack-day.

---

*This document was edited from a Sunday-evening draft to this final version on Wednesday. Click `Original` in the version bar above to read the draft as Marina first wrote it.*
