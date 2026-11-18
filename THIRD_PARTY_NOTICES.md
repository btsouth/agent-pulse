# Third-party notices

## Omarchy

`plugin/Panel.qml`, `plugin/Main.qml`, `plugin/Agent.qml`, the plugin manifest, and plugin assets are derived from Omarchy's Agents plugin and modified for this dashboard. Original copyright: David Heinemeier Hansson. The complete MIT license is in `licenses/Omarchy.txt`.

Source: https://github.com/omacom/omarchy/tree/quattro/shell/plugins/agents

Modifications include a separate plugin identity, dashboard action, selected provider defaults, and a namespaced refresh wrapper. The original project and provider names identify compatibility; they do not imply endorsement.

## LiteLLM

`catalog.json` contains selected numeric pricing fields from `model_prices_and_context_window.json`, outside LiteLLM's enterprise directory. Original copyright: 2023 Berri AI. The complete upstream license notice, including the scope of its MIT license, is in `licenses/LiteLLM.txt`.

Pinned source: https://github.com/BerriAI/litellm/blob/c030ffb6b9c7a1b49072dceca1c2224cdfc21bcc/model_prices_and_context_window.json

## OpenCode Go pricing facts

`pricing.json` records the source URL and verification date for the documented Go model rates. It contains numeric facts and does not reproduce the documentation's prose.

Source: https://opencode.ai/docs/go/
