# Agentic Content · créer un Reel en français

Le modèle propose une idée, deux plans à filmer et une courte narration. Tu filmes les plans et valides les prises. Chatterbox génère un WAV français. Après ton écoute, FFmpeg monte les plans dans l’ordre fourni et ajoute la voix. Tu peux ensuite regarder le MP4 et le poster.

## Installer sur Mac ou Linux

Prérequis : Git, Python 3.11, FFmpeg avec ffprobe et libx264, et une version récente de Codex CLI avec accès au modèle. Sur Mac avec Homebrew :

```sh
brew install python@3.11 ffmpeg
brew install --cask codex
codex login
```

Depuis le clone, lance `./setup.sh`, puis `source .venv/bin/activate`. Le premier appel vocal télécharge les poids publics de Chatterbox depuis Hugging Face. Prévois plusieurs Go disponibles et une connexion pour l’installation et la proposition du modèle. Tu peux aussi définir `CHATTERBOX_CHECKPOINT` avec le chemin d’un checkpoint multilingue v3 complet. La voix incluse suffit.

## Produire une vidéo

```sh
python -m creator idea "Un geste simple à filmer dans ma cuisine"
```

Lis la proposition et copie son identifiant. Filme selon les consignes, regarde les prises, puis lance les commandes depuis le même clone. Remplace les chemins par ceux de tes fichiers.

```sh
ID="identifiant-de-la-proposition"
python -m creator footage "$ID" plan1.mp4 plan2.mov --actor moi
python -m creator voice "$ID"
```

Écoute le WAV au chemin affiché. Pour utiliser ta propre voix, ajoute `--reference ma-voix.wav` à la commande de génération. Lorsque la narration te convient :

```sh
python -m creator listen-ok "$ID" --actor moi
python -m creator render "$ID"
```

Regarde le MP4 vertical 1080 × 1920. Après revue, `python -m creator export-ok "$ID" --actor moi` enregistre ta validation. Tu trouveras le fichier sous `output/projects/<ID>/montage.mp4`. Le montage partage la durée de la voix entre les plans, conserve leur cadrage avec des marges si nécessaire et prolonge la dernière image d’une prise trop courte. La bande-son contient la narration.

Pour reprendre, utilise `python -m creator show "$ID"`. Pour refaire les prises ou la voix avant validation finale, `python -m creator retake "$ID" --actor moi` rouvre la validation du tournage. Tu peux passer une observation de tendance avec `idea --trend "…"`. La génération reçoit le brief et cette observation. Les rushes et la synthèse restent en local.

## Vérifications

`python -m unittest discover -s tests -v` exerce les états de validation avec des fichiers de test. Les sorties, poids et enregistrements personnels restent hors du dépôt. Le parcours ci-dessus utilise le paquet Python `creator`. Le dossier `app` conserve des composants historiques.
