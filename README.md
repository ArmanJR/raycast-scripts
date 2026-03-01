raycast scripts

my collection of [raycast](https://www.raycast.com) scripts that i hacked together

- **read-obsidian-clipping.py** — Searches markdown clippings by keyword, cleans the content, summarizes if needed, translates to casual Persian via OpenRouter, generates TTS audio via ElevenLabs, and auto-plays the result. Caches by default.
  ```
  uv run --script read-obsidian-clipping.py "stranger secret"
  ```

  dependencies: [obsidian](https://obsidian.md) + [clipper extension](https://obsidian.md/clipper), [uv](https://docs.astral.sh/uv), [openrouter](https://openrouter.ai) api key, [elevenlabs](https://elevenlabs.io) api key + voice id
