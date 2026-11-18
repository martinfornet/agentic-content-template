# Agentic Content · créer un Reel en français

Le modèle propose une idée, deux plans à filmer et une courte narration. Tu filmes les plans et valides les prises. Chatterbox génère un WAV français. Après ton écoute, FFmpeg monte les plans dans l’ordre fourni et ajoute la voix. Tu peux ensuite regarder le MP4 et le poster.

## Installer sur Mac ou Linux

Prérequis : Git, Python 3.11, FFmpeg avec ffprobe et libx264, whisper-cli (whisper.cpp), et une version récente de Codex CLI avec accès au modèle. Sur Mac avec Homebrew :

```sh
brew install python@3.11 ffmpeg whisper-cpp
brew install --cask codex
codex login
```

Depuis le clone, lance `./setup.sh`, puis `source .venv/bin/activate`. Le premier appel vocal télécharge les poids publics de Chatterbox depuis Hugging Face. Prévois plusieurs Go disponibles et une connexion pour l’installation et la proposition du modèle. Tu peux aussi définir `CHATTERBOX_CHECKPOINT` avec le chemin d’un checkpoint multilingue v3 complet. La voix incluse suffit. Le premier montage télécharge également ggml-small.bin (488 Mo) pour aligner les sous-titres hors ligne ensuite. Pour réutiliser ce modèle local, définis CREATOR_WHISPER_MODEL. Sous Linux, installe la police DejaVu Sans Bold ou indique un fichier TTF avec CREATOR_FONT.

## Lancer le parcours guidé

Après activation, lance simplement `python -m creator start`. La commande affiche les consignes de tournage, demande les chemins des prises dans l’ordre et attend tes validations. Ouvre le WAV et le MP4 aux chemins indiqués avant de répondre.

## Reprendre ou produire étape par étape

```sh
python -m creator idea "Un geste simple à filmer dans ma cuisine"
```

Lis la proposition et copie son identifiant. Filme selon les consignes, regarde les prises, puis lance les commandes depuis le même clone. Remplace les chemins par ceux de tes fichiers.

```sh
ID="identifiant-de-la-proposition"
python -m creator footage "$ID" plan1.mp4 plan2.mov --actor moi
python -m creator voice "$ID"
python -m creator mixes "$ID"
```

Écoute la voix et les deux préécoutes musicales. Pour utiliser ta propre voix, ajoute `--reference ma-voix.wav` à la commande de génération. Lorsque la narration te convient :

```sh
python -m creator listen-ok "$ID" --actor moi --music A
python -m creator render "$ID"
```

Regarde le MP4 vertical 1080 × 1920. Après revue, `python -m creator export-ok "$ID" --actor moi` enregistre ta validation. Tu trouveras le fichier sous `output/projects/<ID>/montage.mp4`. Le montage rapproche les coupes des groupes de mots et des pulsations de la musique choisie, sans changer l’ordre des prises. Il conserve leur cadrage et prolonge la dernière image d’une prise trop courte. Les sous-titres blancs à contour noir sont incrustés selon le minutage Whisper du WAV. Si le texte et la parole divergent, le montage s’arrête pour permettre une nouvelle prise de voix. Les accompagnements A (90 BPM) et B (120 BPM) sont synthétisés localement. Pour garder la voix seule, omets --music.

Pour reprendre, utilise `python -m creator show "$ID"`. Pour refaire les prises ou la voix avant validation finale, `python -m creator retake "$ID" --actor moi` rouvre la validation du tournage. Tu peux passer une observation de tendance avec `idea --trend "…"`. La génération reçoit le brief et cette observation. Les rushes et la synthèse restent en local.

## Vérifications

`python -m unittest discover -s tests -v` exerce les états de validation avec des fichiers de test. Les sorties, poids et enregistrements personnels restent hors du dépôt. Le parcours ci-dessus utilise le paquet Python `creator`. Le dossier `app` conserve des composants historiques.

Pour reproduire le test complet après installation, exécute `./scripts/test-mp4.sh`. Il appelle réellement le modèle, fabrique deux prises de couleur, synthétise le texte et décode le MP4. Les décisions du scénario sont enregistrées sous le nom `test-technique`. Le fichier reste disponible pour écoute et visionnage.
