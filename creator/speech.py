"""CPU synthesis with the multilingual v3 checkpoint and its included voice."""
import os
from pathlib import Path

REVISION = "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18"


def synthesize(script, output, reference=None):
    import torch
    import torchaudio
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    from huggingface_hub import snapshot_download

    checkpoint = os.environ.get("CHATTERBOX_CHECKPOINT") or snapshot_download(
        "ResembleAI/chatterbox", revision=REVISION,
        allow_patterns=["ve.pt", "s3gen.pt", "t3_mtl23ls_v3.safetensors", "conds.pt",
                        "grapheme_mtl_merged_expanded_v1.json", "Cangjie5_TC.json"])
    torch.set_num_threads(4)
    model = ChatterboxMultilingualTTS.from_local(Path(checkpoint), "cpu", t3_model="v3")
    wav = model.generate(script, language_id="fr", audio_prompt_path=reference)
    torchaudio.save(str(output), wav.cpu(), model.sr, encoding="PCM_S", bits_per_sample=16)
