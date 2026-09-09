# Taste
- Communicates in Chinese (Simplified Chinese); conversations should be conducted in Chinese. Confidence: 0.9
- When adding new entries to a config file, prefers mirroring the structure/fields of an existing similar entry (e.g., "参考 Niko") rather than inventing a new format. Confidence: 0.7
- Expects concrete values (e.g., API base URLs, model IDs) to be researched from official/authoritative sources and filled in accurately, not guessed or left as placeholders. Confidence: 0.6
- Prefers writing a fresh, self-contained script from scratch rather than reusing/modifying an existing script, when a specific tool is needed (e.g., "你自己写一个脚本，不要用其他已有的"). Confidence: 0.6
- Works on Linux (home `/home/lzy`); VS Code user-level configs live under `~/.config/Code/User/` (e.g., `chatLanguageModels.json` for custom OpenAI-compatible model providers). Confidence: 0.7
- Uses custom-endpoint model providers in VS Code (OpenAI chat-completions style), preferring API keys stored via VS Code's secret mechanism (`${input:chat.lm.secret.*}` placeholders) over plaintext keys in config files. Confidence: 0.6
