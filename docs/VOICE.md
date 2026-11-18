# Voice generation and review

The local historical code used Chatterbox Multilingual from a local checkpoint, conditioned on a reference recording and configured for French. The generic workflow keeps the engine configurable and keeps checkpoints and reference recordings outside Git.

A voice candidate should retain the exact text, spoken text, engine/version, settings, seed if supported, reference identity, output hash, and review record. Human review must listen to the exact audio. A transcript, signal metric, or automated score cannot establish naturalness or replace listening. No dedicated automated voice moderation component is included by this template.
