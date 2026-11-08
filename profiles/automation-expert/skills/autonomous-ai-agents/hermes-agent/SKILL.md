---
name: hermes-agent
description: "Sin uso en este profile."
platforms: [ninguna]
---

Hermes siembra esta skill en cada arranque, aunque el profile tenga el marker .no-bundled-skills
(tools/skills_sync.py, ESSENTIAL_SKILLS). Una copia propia en el mismo lugar hace que no la toque, y
`platforms: [ninguna]` la deja afuera del indice. El Automation Expert no la puede leer (ver
`skill_view` del plugin automation-expert-read).
