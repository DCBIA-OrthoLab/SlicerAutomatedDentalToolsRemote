# Dé-identification côté client - module 3D Slicer
## Spécification de conception - `slicer_client/`

*Projet `VISOR-serve` - University of North Carolina at Chapel Hill*
*Version 1.0 - juillet 2026*

---

## 0. Résumé

Aujourd'hui, `inference_client.py` prend un `file_path` et le streame tel quel vers le serveur. **Il n'existe aucun traitement de dé-identification.** Le commentaire du serveur (« de-identification happens client-side ») décrit donc une intention, pas une réalité : en l'état, de la PHI brute part sur le réseau.

Ce document spécifie ce qu'il faut construire côté client. Trois idées directrices :

1. **Un point d'étranglement unique.** Tous vos outils (15+, et le nombre va croître) passent par `run_tool()`. La dé-identification doit s'insérer là, et nulle part ailleurs. Un outil ne doit pas pouvoir contourner la barrière, et ajouter un outil ne doit rien exiger de son auteur. C'est exactement la philosophie de votre registry côté serveur, transposée au client.
2. **Fail-closed.** Si le pipeline échoue, plante, ou détecte un doute, **l'envoi est annulé**. Jamais d'avertissement suivi d'un envoi. Jamais de case à cocher « j'ai vérifié ».
3. **La dé-identification produit une preuve.** Chaque envoi génère un manifeste local horodaté : quel profil, quelle version, quels contrôles passés. C'est ce qui vous protège en cas d'audit et c'est ce que l'OHRE demandera.

Le gros du travail n'est pas cryptographique. Il est dans l'**inventaire exhaustif des fuites**, et 3D Slicer en a beaucoup plus qu'on ne l'imagine - noms de nœuds, logs applicatifs, base DICOM locale, noms de fichiers, scènes de récupération après crash. La section 4 est la partie la plus importante de ce document.

---

## 1. Cadre institutionnel UNC - à régler avant d'écrire du code

### 1.1 Qui est qui

- **UNC-Chapel Hill est le Covered Entity.** Le poste de travail Slicer, dans un service clinique ou un laboratoire UNC, détient légitimement de la PHI.
- **Le serveur d'inférence est le Business Associate**, même s'il appartient à la même institution : si l'entité qui l'opère est distincte de l'unité couverte, un BAA ou un accord interne équivalent est nécessaire. Faites trancher ce point par le UNC Privacy Office.
- **Vous, développeurs, êtes des membres du personnel de recherche.** Vous n'avez pas accès à la PHI par défaut.

### 1.2 Le préalable réglementaire

<cite index="14-1">UNC-Chapel Hill dispose d'un cadre spécifique de reclassification de la PHI en « Research Health Information » (RHI), via trois voies : une autorisation HIPAA approuvée par l'IRB, un waiver partiel, ou un waiver complet d'autorisation.</cite> <cite index="15-1">À UNC-Chapel Hill, l'Office of Human Research Ethics (OHRE) est l'instance désignée pour statuer sur les demandes de waiver ou d'altération de l'exigence d'autorisation.</cite>

Concrètement, **avant le premier transfert** :

- Protocole IRB approuvé par l'OHRE, couvrant explicitement le transfert vers un serveur de calcul distant
- Autorisation HIPAA signée, ou waiver
- <cite index="12-1">Formation CITI complétée, module « Research and HIPAA Privacy Protections » inclus - cette formation s'ajoute à la formation HIPAA de UNC Health Care et ne la remplace pas</cite>
- Validation de l'architecture par le UNC Information Security Office

Point important : <cite index="11-1">selon les SOP de l'IRB de UNC-Chapel Hill, pour être considérées comme dé-identifiées, les 18 catégories d'identifiants HIPAA doivent être supprimées - y compris toutes les dates, comme les dates d'intervention, et toutes les images photographiques.</cite> C'est une lecture stricte, et elle a deux conséquences directes sur votre pipeline : **aucune date ne survit au Safe Harbor** (voir §3.6), et les **images faciales sont explicitement visées** (voir §3.7).

### 1.3 Choisir un régime - par outil, pas globalement

C'est la décision structurante. Trois régimes, trois architectures différentes :

| Régime | Ce qui survit | Le serveur voit-il de la PHI ? | Formalités |
|---|---|---|---|
| **A. Safe Harbor** (§164.514(b)(2)) | Aucun des 18 identifiants. Ni dates, ni visage, ni numéro de série d'appareil | **Non** → serveur hors périmètre HIPAA | Aucune formalité supplémentaire une fois le pipeline validé |
| **B. Limited Data Set** (§164.514(e)) | Dates complètes, ville/état/code postal. Pas d'identifiants directs | **Oui**, un LDS reste de la PHI | **Data Use Agreement** obligatoire + serveur en périmètre complet |
| **C. PHI avec waiver IRB** | Tout | Oui | BAA + waiver + toutes les garanties de la Security Rule |

**Recommandation : viser le régime A partout où c'est possible, et n'accepter le régime B que par exception documentée.** La différence de coût entre A et B est énorme : le régime A vous dispense du BAA, de l'audit trail complet côté serveur, du chiffrement au repos réglementaire, du plan de continuité, de la notification de brèche.

**Mais attention au cas `surg_mov_pred`.** Prédiction de mouvement en chirurgie orthognathique : l'outil a besoin des structures faciales, donc le defacing est impossible. La donnée reste identifiable au sens de la catégorie 17. **Cet outil ne peut pas être en régime A.** Il relève du régime B ou C, avec une Expert Determination si vous voulez le faire basculer.

**Conséquence architecturale : le régime doit être un attribut de l'outil, pas une propriété globale du système.** Chaque `Tool` déclare son régime ; le client applique le profil de dé-identification correspondant ; le serveur sait dans quel régime il opère et applique les garanties adaptées. Cela s'exprime naturellement dans votre design existant, sans toucher au core.

### 1.4 La position des développeurs français

Vous développez depuis la France sur un projet américain. Deux règles absolues :

- **Jamais de PHI de patients UNC sur un poste de développement en France.** Ce serait une divulgation non autorisée et, accessoirement, un transfert international de données de santé.
- **Le développement et les tests se font exclusivement sur des jeux publics** (voir §10). Le debug sur données réelles, si nécessaire, se fait par un membre de l'équipe habilité, à UNC, sur un poste UNC, avec journalisation.

Si des patients européens devaient entrer dans le périmètre un jour, le RGPD et la certification HDS s'ajouteraient - mais ce n'est pas le cas ici.

---

## 2. Architecture : trois barrières et un point d'étranglement

### 2.1 Les trois barrières

```
   Données cliniques (PACS, disque, CD)
              │
   ┌──────────▼──────────┐
   │  BARRIÈRE A         │   Ingestion contrôlée
   │  Module « Data Prep »│   Anonymisation par lot, inspection, workspace propre
   └──────────┬──────────┘
              │
        Espace de travail « clean »
              │
   ┌──────────▼──────────┐
   │  BARRIÈRE B         │   ★ LE POINT D'ÉTRANGLEMENT ★
   │  run_tool()         │   Non contournable, fail-closed, applique le profil de l'outil
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │  BARRIÈRE C         │   Vérification post-traitement
   │  Self-check         │   Re-scan de l'artefact produit AVANT tout appel réseau
   └──────────┬──────────┘
              │
        HTTPS ──────────► Serveur (portier serveur = 4ᵉ barrière, indépendante)
```

**La barrière B est la seule obligatoire.** A et C la renforcent. La raison de placer l'obligation en B et pas en A : la barrière A dépend de la discipline de l'utilisateur, la barrière B est du code que personne ne peut sauter.

### 2.2 Insertion dans `inference_client.py`

Le principe, sans code : `run_tool()` ne reçoit plus un chemin qu'il streame. Il reçoit un chemin, le confie à un préparateur de charge utile, et **ne streame que ce que le préparateur lui rend**. Le fichier d'origine n'est jamais ouvert par la couche réseau.

Trois propriétés à garantir :

1. **Aucun chemin alternatif.** Il ne doit exister aucune fonction publique du client capable d'envoyer un fichier sans passer par le préparateur. Si vous exposez un `_post_raw()` « pour le debug », il finira en production.
2. **Fail-closed strict.** Toute exception du préparateur remonte et annule l'envoi. Pas de `except: pass`, pas de mode dégradé, pas de paramètre `skip_deid=True` - même « juste pour les tests », même caché.
3. **L'artefact dé-identifié vit dans un répertoire temporaire dédié**, supprimé en `finally`, y compris en cas d'erreur réseau. Même exigence que côté serveur.

Le préparateur retourne trois choses : le chemin de l'artefact propre, un nom de fichier neutre à envoyer, et un manifeste.

### 2.3 Pourquoi ça ne casse pas votre généricité

Votre contrainte « ajouter un outil = un fichier, rien d'autre » est préservée : l'auteur d'un outil déclare un **profil** (une chaîne, par exemple `"safe_harbor_ct"`), et le client applique la recette correspondante. Il n'écrit aucune logique de dé-identification. Les profils sont des fichiers de configuration déclaratifs, versionnés dans le dépôt, revus comme du code.

---

## 3. Le pipeline de dé-identification, étape par étape

### 3.1 Cadrage par liste blanche

Avant toute chose, **restreindre ce qui peut entrer**. Une liste blanche de modalités DICOM autorisées par outil (`CT`, `MR`, `CBCT`…) et un rejet explicite du reste. Cela élimine d'emblée les modalités à haut risque de texte incrusté (`US`, `XA`, `SC`, `OT`, captures d'écran de console) plutôt que d'essayer de les nettoyer. Refuser est toujours plus sûr que nettoyer.

Même logique pour les objets structurés : `SR` (Structured Report), `KO`, `PR` contiennent du texte libre rédigé par un radiologue - quasi impossible à nettoyer de façon fiable. Rejetez-les.

### 3.2 Normalisation du conteneur

Convertir en un format canonique avant nettoyage, pour ne pas avoir 15 chemins de code. Deux stratégies :

- **DICOM → DICOM** : on reste en DICOM, on nettoie les tags. Conserve toute l'information clinique, mais la surface d'attaque (private tags, séquences) est large.
- **DICOM → NIfTI** : la conversion elle-même détruit la quasi-totalité des métadonnées. C'est un puissant réducteur de risque « gratuit ». Mais attention : `dcm2niix` génère par défaut un **fichier JSON sidecar BIDS** qui recopie une bonne partie des métadonnées DICOM, y compris `PatientName` selon les options. Désactivez-le ou nettoyez-le.

**Recommandation** : privilégiez la conversion en NIfTI quand l'outil n'a pas besoin des métadonnées DICOM. C'est le chemin le plus court vers le régime A. Gardez le DICOM natif seulement pour les outils qui en dépendent réellement.

### 3.3 Nettoyage des métadonnées DICOM

Ne construisez pas votre propre liste de tags. Implémentez le **profil DICOM PS3.15 Annexe E** (*Basic Application Level Confidentiality Profile*) et choisissez explicitement les options que vous activez :

| Option PS3.15 | Effet | Recommandation |
|---|---|---|
| Clean Descriptors | Nettoie les champs texte libre (`StudyDescription`, `SeriesDescription`, commentaires) | **Activer** |
| Retain Longitudinal Temporal (Full / Modified) | Conserve les dates, ou les décale | Régime B uniquement |
| Retain Patient Characteristics | Conserve âge, sexe, poids, taille | Activer avec prudence : âge > 89 doit être agrégé en « 90+ » |
| Retain Device Identity | Conserve modèle, numéro de série, `StationName` | **Ne pas activer** - catégorie 16 du Safe Harbor |
| Retain Institution Identity | Conserve nom de l'établissement | **Ne pas activer** |
| Retain UIDs | Conserve les UIDs d'origine | **Ne pas activer** - voir §3.5 |
| Retain Safe Private | Conserve les private tags « connus sûrs » | Ne pas activer en régime A |
| Clean Pixel Data | Masque le texte incrusté | Voir §3.7 |

Deux tags méritent d'être ajoutés à la main car ils sont régulièrement oubliés : `PatientComments`, `AdditionalPatientHistory`, `RequestAttributesSequence`, `OtherPatientIDs`, et `IssuerOfPatientID`.

### 3.4 Private tags et séquences imbriquées

**Les deux pièges classiques.**

**Private tags** : chaque constructeur (Siemens, GE, Philips, Canon, Planmeca, Carestream pour le CBCT dentaire) ajoute des blocs propriétaires non documentés. On y trouve des noms d'opérateur, des chemins réseau, des identifiants d'examen, parfois des copies de champs standard. **Règle : suppression totale par défaut.** Ne réintroduisez un bloc privé qu'après inspection manuelle documentée, et uniquement s'il est indispensable au traitement.

**Séquences imbriquées (`SQ`)** : c'est la fuite la plus fréquente dans les implémentations maison. Un `PatientName` peut apparaître trois niveaux plus bas dans une `ReferencedImageSequence` ou une `SourceImageSequence`, parfaitement invisible d'un nettoyage de surface. **Le nettoyage doit être récursif, sans limite de profondeur.** Testez-le explicitement : c'est le premier bug que votre validation doit attraper.

### 3.5 Remappage des UIDs

`StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID`, `FrameOfReferenceUID` permettent de recorréler avec le PACS d'origine : ce sont des identifiants au sens de la catégorie 18.

La technique : `nouvel_UID = racine_UID_de_l'organisation + tronqué(HMAC-SHA256(clé_secrète, UID_original))`, formaté selon les contraintes DICOM (≤ 64 caractères, chiffres et points, pas de zéro non significatif).

Deux propriétés à respecter impérativement :

- **Déterminisme** : le même UID d'entrée donne toujours le même UID de sortie, sinon deux envois successifs de la même série produisent des données incohérentes.
- **Cohérence relationnelle** : toutes les références croisées doivent être remappées ensemble. Si un `FrameOfReferenceUID` est remappé différemment entre deux séries d'une même étude, **tout recalage spatial casse**. C'est un bug fonctionnel silencieux et pénible à diagnostiquer.

La clé HMAC est un secret détenu par UNC (voir §6). Générer des UIDs aléatoires est plus simple mais casse le déterminisme - ne le faites que si vous n'avez jamais besoin de renvoyer deux fois la même donnée.

### 3.6 Le traitement des dates - le point le plus mal compris

<cite index="11-1">La lecture de l'IRB de UNC est explicite : toutes les dates doivent être supprimées, y compris les dates d'intervention.</cite> Sous le régime A, il n'y a pas d'aménagement : `StudyDate`, `SeriesDate`, `AcquisitionDate`, `ContentDate`, `PatientBirthDate`, et leurs équivalents `*Time` disparaissent. Seule l'**année** peut survivre dans certaines lectures, et l'**âge** en années (agrégé à « 90+ » au-delà de 89 ans).

**Le date shifting - décaler toutes les dates d'un patient d'un offset aléatoire fixe, ce qui préserve les intervalles - ne relève PAS du Safe Harbor.** C'est une technique excellente et largement utilisée, mais elle conserve de l'information temporelle et relève donc soit du Limited Data Set (régime B), soit d'une Expert Determination. Ne la présentez jamais comme du Safe Harbor à votre IRB : c'est une erreur fréquente et elle sera relevée.

Si votre outil a besoin d'intervalles temporels (suivi longitudinal, prédiction post-opératoire), **exprimez-les en durées relatives** (« J+42 » plutôt que deux dates). Une durée n'est pas une date. C'est souvent suffisant fonctionnellement et ça vous garde en régime A.

### 3.7 Les pixels : texte incrusté et visage

**Texte incrusté.** Le tag `BurnedInAnnotation` est peu fiable - souvent absent, souvent `NO` à tort. Trois approches :
1. **Exclusion par modalité** (§3.1) - la plus sûre, la moins coûteuse. Privilégiez-la.
2. **Masquage de régions fixes** connues par constructeur/protocole - fiable si vos protocoles d'acquisition sont standardisés, ce qui est souvent le cas en recherche.
3. **OCR** (`pytesseract`, `easyocr`) - dernier recours, coûteux, jamais fiable à 100 %. À ne jamais utiliser seul comme unique garantie.

**Visage reconstructible.** C'est le point critique pour vous, puisque vous manipulez des volumes crâniens. Un CT ou une IRM de la tête permet la reconstruction d'une surface faciale identifiable par reconnaissance automatique. <cite index="11-1">L'IRB de UNC vise explicitement « toutes les images photographiques » dans son énumération des 18 identifiants.</cite>

| Technique | Ce qui est détruit | Coût | Compatible avec un outil orthognathique ? |
|---|---|---|---|
| Skull stripping | Tout hors cerveau | Modéré | **Non** |
| `pydeface` / `mri_deface` | Voxels du visage | 30 s – 3 min/volume | **Non** |
| `quickshear` | Demi-espace facial | 2–10 s/volume | **Non** |
| `@afni_refacer` | Remplace par un visage synthétique | 1–5 min/volume | Partiellement - à valider cliniquement |

**Pour un outil comme `surg_mov_pred`, aucune de ces techniques n'est acceptable**, puisque la géométrie faciale *est* le signal. Conclusion : cet outil ne peut pas atteindre le régime A par voie technique. Documentez ce constat, et traitez-le en régime B avec DUA, ou faites financer une Expert Determination.

**Ne cachez pas ce point à l'IRB.** Un pipeline qui nettoie parfaitement les tags et laisse partir un visage reconstructible en prétendant faire du Safe Harbor est un problème sérieux si l'OCR le découvre après un incident.

### 3.8 Pseudonymisation et renommage

- L'identifiant patient devient `tronqué(HMAC-SHA256(clé_UNC, MRN))`. Déterministe, non inversible sans la clé.
- **La table de correspondance pseudonyme ↔ patient reste à UNC, chez le Covered Entity, jamais transmise, jamais sauvegardée avec les données.** Si le serveur peut faire la correspondance, la donnée redevient de la PHI (§164.514(c)).
- **Le nom du fichier envoyé doit être neutre et généré côté client.** C'est une fuite massive et systématiquement oubliée : `SMITH_John_CBCT_pre-op_2024-03-12.nii.gz` contient un nom, une date et un contexte clinique - et il part dans l'en-tête `Content-Disposition`, dans les logs du reverse proxy, dans les métriques. Générez un UUID ou un dérivé du pseudonyme. Idem pour les noms de répertoires dans une archive.

### 3.9 Vérification post-traitement (barrière C)

Ne faites jamais confiance à votre propre pipeline. Après production de l'artefact, avant tout appel réseau, **re-scannez le résultat** :

- Re-parsing complet et récursif : aucun tag de la liste noire présent ou non vide
- Aucun private tag résiduel
- Aucun UID d'origine (comparaison avec la source)
- Aucune date au-delà de la granularité autorisée
- Le nom de fichier est bien le nom neutre généré
- Recherche de motifs dans tous les champs texte restants : chaînes ressemblant à un MRN, à un SSN, à un nom propre - `presidio-analyzer` fait ça bien
- Taille et dimensions cohérentes avec la source (détection d'une conversion ratée)

En cas d'échec : **abandon, message à l'utilisateur, entrée dans le journal local, aucun envoi**. Et remontée à l'équipe : un échec de vérification signale un bug du pipeline, donc potentiellement des envois antérieurs défectueux.

### 3.10 Manifeste et journal local

Chaque envoi produit un enregistrement local, conservé sur le poste ou centralisé chez UNC :

- Horodatage UTC
- Identité de l'opérateur
- Pseudonyme (jamais le MRN)
- Empreinte SHA-256 de l'artefact envoyé
- Nom et **version** du profil appliqué
- Version du code du pipeline (hash de commit)
- Liste des contrôles de vérification passés
- Outil cible et régime déclaré

Le versionnage du profil et du code est essentiel : si vous découvrez dans six mois qu'un profil laissait fuir un tag, ce journal vous dit **exactement** quels envois sont concernés. Sans lui, vous devez présumer que tout est compromis.

Ce manifeste ne doit contenir **aucune PHI**. C'est un journal de conformité, pas un journal clinique.

---

## 4. Les fuites spécifiques à 3D Slicer

**C'est la section la plus importante de ce document.** Un pipeline DICOM parfait ne sert à rien si Slicer laisse fuir la PHI par ailleurs. Slicer est un environnement de recherche, pas un outil clinique durci : il conserve du contexte partout.

### 4.1 Les noms de nœuds MRML

Quand on charge une série DICOM, Slicer nomme les nœuds à partir de `SeriesDescription` et parfois de `PatientName`. Un `vtkMRMLScalarVolumeNode` peut littéralement s'appeler `SMITH^John_CBCT`. Si un outil envoie une scène, un segment, ou un fichier exporté dont le nom dérive du nom du nœud, la PHI part avec.

**À faire** : renommer systématiquement les nœuds au moment de l'ingestion (barrière A) avec le pseudonyme, et **ne jamais utiliser un nom de nœud pour construire un nom de fichier envoyé**.

### 4.2 Les attributs de nœuds

Slicer attache des attributs aux nœuds, notamment `DICOM.instanceUIDs`, qui contient la liste des UIDs d'origine. Ils survivent à l'export de la scène. Purgez-les explicitement.

### 4.3 La base DICOM locale de Slicer

`ctkDICOMDatabase` (par défaut sous `~/Documents/SlicerDICOMDatabase`) stocke une **copie complète et non anonymisée** de tout ce qui a été importé, dans une base SQLite en clair. C'est un stock de PHI persistant sur le poste, souvent oublié lors de la mise au rebut d'une machine.

**À faire** : politique explicite - soit la base est sur un volume chiffré, soit elle est purgée après chaque session, soit l'ingestion se fait hors base DICOM.

### 4.4 Les logs applicatifs de Slicer

`Slicer.log` et `SlicerLastSession.log` enregistrent les **chemins complets des fichiers chargés**. Ces chemins contiennent presque toujours le nom du patient. C'est de la PHI dans un fichier texte non protégé, conservé indéfiniment.

**À faire** : ne jamais transmettre ces logs (support technique inclus - un développeur français qui demande « envoyez-moi le log » demande de la PHI), et prévoir une purge. Votre propre code ne doit jamais écrire de chemin d'entrée dans un log.

### 4.5 Les fichiers récents et les préférences

Le fichier de configuration Slicer (`.ini`) conserve la liste des fichiers récemment ouverts et les répertoires par défaut. Mêmes fuites que ci-dessus.

### 4.6 Les scènes de récupération après crash

Slicer sauvegarde automatiquement des snapshots de scène pour la récupération après plantage, dans le répertoire temporaire de l'application. Ces fichiers survivent aux crashs - c'est leur raison d'être - et contiennent la scène complète, PHI incluse.

### 4.7 Le répertoire temporaire de l'application

`slicer.app.temporaryPath` est partagé par tous les modules et toutes les extensions installées. **N'y écrivez jamais vos artefacts intermédiaires de dé-identification.** Créez votre propre répertoire temporaire, avec des permissions restrictives, supprimé en `finally`.

### 4.8 Les captures d'écran et les vues

Toute capture d'écran de Slicer affiche potentiellement le nom du patient dans le coin de la vue (annotations DICOM) et dans l'arbre des données. À proscrire dans les tickets, les présentations et la documentation - sauf sur données publiques.

### 4.9 Les fichiers de scène `.mrml` et `.mrb`

Un `.mrml` contient les **chemins absolus** de tous les fichiers référencés. Un `.mrb` est une archive de la scène complète. Ni l'un ni l'autre ne doit être envoyé au serveur sans traitement dédié.

### 4.10 Checklist d'ingestion (barrière A)

À l'import, avant que la donnée ne devienne manipulable :
- [ ] Renommer tous les nœuds avec le pseudonyme
- [ ] Purger les attributs `DICOM.*` des nœuds
- [ ] Vérifier qu'aucun nœud de la scène ne porte de nom d'origine
- [ ] Travailler dans un répertoire de travail dédié, hors `Documents`
- [ ] Base DICOM sur volume chiffré ou purgée en fin de session

---

## 5. Les formats dérivés

| Format | Fuites | Traitement |
|---|---|---|
| **NIfTI** (`.nii`, `.nii.gz`) | Champ `descrip` du header (libre), `aux_file`, `intent_name` | Réécrire le header ; vider `descrip` |
| **JSON sidecar BIDS** | Recopie massive des métadonnées DICOM (`dcm2niix`) | Désactiver la génération, ou filtrer par liste blanche stricte |
| **`.mrml` / `.mrb`** | Chemins absolus, noms de nœuds, scène complète | Ne pas transmettre |
| **VTK / STL / OBJ** (surfaces, segmentations) | En-tête de commentaire libre, nom de fichier | Réécrire l'en-tête ; renommer |
| **Segmentations** (`.seg.nrrd`) | Noms de segments dérivés du contexte clinique, champs de métadonnées NRRD | Renommer les segments ; filtrer les clés NRRD |
| **CSV / XLSX** (sorties de `surg_mov_pred`) | Colonnes d'identifiants, noms de feuilles, **métadonnées de l'auteur du fichier** | Vérifier au retour aussi bien qu'à l'aller |
| **Archives ZIP** | Noms de fichiers et de répertoires internes, horodatages | Reconstruire l'archive avec des noms neutres |

**Point souvent oublié : le flux retour.** Un fichier `.xlsx` produit par le serveur et rouvert dans Slicer n'est pas un risque de fuite vers l'extérieur, mais si l'utilisateur le partage, ses métadonnées d'auteur et ses noms de feuilles peuvent porter du contexte. Prévoyez la question.

---

## 6. Gestion des secrets côté client

Trois secrets vivent sur le poste client, avec des sensibilités différentes :

| Secret | Rôle | Où le stocker |
|---|---|---|
| **Clé HMAC de pseudonymisation** | Génère les pseudonymes et les UIDs remappés | **Le plus sensible.** Trousseau du système d'exploitation (`keyring` : Keychain macOS, Credential Manager Windows, Secret Service Linux). Jamais dans les settings Slicer, jamais dans un `.env`, jamais dans le dépôt |
| **Jeton / identifiants d'accès au serveur** | Authentification | Trousseau OS également ; idéalement remplacé par un flow OIDC où rien de durable n'est stocké hors refresh token |
| **Table de correspondance** | Retrouver le patient à partir du pseudonyme | Ne vit **pas** dans le module Slicer. Système séparé, contrôlé par UNC, accès restreint - c'est le rôle typique d'un *honest broker* |

<cite index="13-1">Le rôle d'honest broker consiste précisément à faire écran entre l'investigateur et les informations identifiantes des sujets, en générant ou recevant un jeu de données puis en le dépouillant de ses identifiants.</cite> Si UNC dispose d'un tel service, appuyez-vous dessus : la table de correspondance n'est alors ni votre problème ni votre responsabilité, ce qui est une excellente nouvelle.

**Erreurs à ne pas commettre** : dériver la clé HMAC d'une constante du code (annule tout l'intérêt) ; utiliser la même clé pour tous les établissements (permet la corrélation croisée) ; stocker la clé à côté des données pseudonymisées.

Précision importante : les pseudonymes HMAC sont vulnérables à une **attaque par dictionnaire** si l'espace des entrées est petit. Un MRN à 8 chiffres, c'est 10⁸ possibilités - trivial à énumérer si la clé fuit. La clé est donc le point de défaillance unique de tout le schéma. Traitez-la comme telle.

---

## 7. Contraintes techniques dans l'environnement Slicer

Le client limite ses dépendances à `requests`, `slicer`, `qt`, `vtk`, `os`, `tempfile`. **Cette contrainte n'est pas tenable pour de la dé-identification** : il faut la desserrer de façon contrôlée.

### 7.1 Ce dont vous disposez déjà

- **`pydicom`** est embarqué dans les versions récentes de Slicer 5.x (vérifiez par un simple `import pydicom` sur votre version cible). C'est votre outil principal.
- **`numpy`** est embarqué.
- **DCMTK** est livré avec Slicer sous forme de binaires (`dcmdump`, `dcmodify`, `dcmconv`…) dans le répertoire `bin`. Utilisable en sous-processus, très robuste, et sans ajout de dépendance Python.
- **CTK / `ctkDICOMDatabase`** est accessible depuis Python.
- **SimpleITK** est disponible via l'extension du même nom.
- Le module **DICOM Patcher** de Slicer propose une option d'anonymisation. **Elle est insuffisante pour un usage HIPAA** - pas de profil PS3.15 complet, pas de gestion cohérente des UIDs, pas de vérification. Ne vous appuyez pas dessus, mais elle peut servir de référence de lecture.

### 7.2 Installation de dépendances supplémentaires

`slicer.util.pip_install()` fonctionne et installe dans l'environnement Python de Slicer. Trois précautions :

1. **Installer au premier lancement du module, pas à l'import**, avec un indicateur de progression - sinon Slicer semble figé.
2. **Épingler les versions** et vérifier les empreintes. Une dépendance de dé-identification compromise est un scénario d'attaque évident.
3. **Prévoir le cas hors ligne** : beaucoup de postes cliniques n'ont pas d'accès Internet sortant. Prévoyez un mode d'installation par paquet local, ou packagez les dépendances dans l'extension Slicer.

### 7.3 Dépendances recommandées

| Bibliothèque | Rôle | Poids |
|---|---|---|
| `pydicom` | Lecture/écriture DICOM | Déjà présent |
| `dicognito` | Anonymisation cohérente, remappage d'UID | Léger |
| `deid` (Stanford) | Recettes déclaratives, gestion des pixels | Moyen |
| `dicom-anonymizer` | Implémentation des actions PS3.15 | Léger |
| `presidio-analyzer` | Détection de PHI dans les champs texte | Lourd (spaCy) - envisager un mode dégradé par expressions régulières |
| `nibabel` | Nettoyage de headers NIfTI | Léger |
| `keyring` | Stockage des secrets dans le trousseau OS | Léger |
| `pydeface` / `quickshear` | Defacing (si applicable) | Lourd, dépendances externes (FSL) |

**Note sur le defacing** : `pydeface` dépend de FSL, qui n'est pas trivialement installable sur Windows. Si vos utilisateurs sont sous Windows, envisagez `quickshear` (Python pur, plus rapide, moins fin) ou une exécution du defacing côté serveur - mais côté serveur signifie que le visage a déjà traversé le réseau, donc régime B au minimum. C'est un arbitrage à documenter.

---

## 8. Le profil déclaratif par outil

Plutôt que du code de dé-identification dispersé, un **fichier de recette** par profil, versionné dans le dépôt :

- Régime visé (A / B / C)
- Modalités autorisées (liste blanche)
- Format de sortie (DICOM natif / NIfTI)
- Options PS3.15 activées
- Politique de dates
- Politique de pixels (aucun / masquage régions / OCR)
- Politique de visage (aucun / deface / reface)
- Contrôles de vérification obligatoires
- Version du profil

Bénéfices : le profil est **relisible par un non-développeur** - votre IRB et votre Privacy Office peuvent l'auditer sans lire du Python ; il est versionné, donc traçable ; et un nouvel outil réutilise un profil existant sans écrire de code.

Chaque `Tool` déclare son profil. Le serveur connaît aussi ce profil et peut vérifier la cohérence entre le régime déclaré et ce qu'il reçoit.

---

## 9. Ergonomie : rendre le pipeline non contournable *et* acceptable

Un contrôle de sécurité que les utilisateurs contournent est pire qu'aucun contrôle, parce qu'il donne une fausse assurance.

**À faire :**
- Anonymisation **par lot en tâche de fond**, au moment de l'ingestion - pas à chaque envoi. L'utilisateur prépare son jeu de données une fois.
- **Cache** de l'artefact dé-identifié, indexé par empreinte de la source + version du profil. Un second envoi du même volume est instantané. Purge automatique en fin de session.
- **Indicateur visuel permanent** : la scène affiche « données dé-identifiées - profil safe_harbor_ct v3 » ou « ⚠ données identifiantes - envoi impossible ». L'état doit être évident, jamais à deviner.
- **Messages d'erreur actionnables** : « envoi bloqué : le tag PatientBirthDate est encore présent - relancez la préparation ». Pas « erreur 422 ».
- **Aperçu avant envoi** : liste des champs supprimés et conservés, consultable. Ça crée la confiance et ça permet à l'utilisateur de repérer un problème que le code n'a pas vu.

**À ne pas faire :**
- Une case « j'atteste avoir anonymisé »
- Un bouton « envoyer quand même »
- Un mode debug qui contourne la barrière
- Un message d'erreur qui affiche la valeur identifiante détectée (vous transformez votre UI en canal de fuite, et le message finit dans une capture d'écran envoyée par mail)

---

## 10. Validation du pipeline : comment prouver qu'il fonctionne

C'est ce qui distingue un pipeline crédible d'un pipeline déclaratif. Sans validation documentée, vous ne pouvez rien affirmer à l'IRB.

### 10.1 Jeux de données de test

- **TCIA « Pseudo-PHI-DICOM-Data »** : collection publique conçue précisément pour tester les outils de dé-identification - de la fausse PHI est injectée dans les tags *et* dans les pixels, avec une vérité terrain. C'est votre jeu de référence principal, et il est utilisable par les développeurs français sans aucun problème réglementaire.
- **MIDI (Medical Image De-Identification) benchmark** du NCI, si accessible.
- **Vos propres cas adversariaux** : PHI en séquence imbriquée profonde, private tags Planmeca/Carestream pour le CBCT, encodages non-ASCII (`PatientName` en UTF-8 avec caractères accentués - cas typiquement mal géré), champs multi-valués, `PatientName` en composants (`Nom^Prénom^Milieu^Préfixe^Suffixe`).

### 10.2 Tests automatisés obligatoires

- **Test de non-régression sur chaque profil** : pour chaque profil, un jeu d'entrée connu doit produire une sortie dont l'empreinte est stable. Toute modification du profil doit être intentionnelle et revue.
- **Test de récursivité** : PHI enfouie à 4 niveaux de séquence → doit être détectée.
- **Test de cohérence des UIDs** : deux séries d'une même étude → `FrameOfReferenceUID` identique après remappage.
- **Test de déterminisme** : deux exécutions → même sortie.
- **Test fail-closed** : injecter une panne (fichier corrompu, disque plein, dépendance absente) → vérifier qu'aucun appel réseau n'est émis. Testez-le en interceptant la couche réseau.
- **Test de bout en bout** : simuler un envoi et inspecter le payload HTTP capturé, y compris les en-têtes et le nom de fichier.

### 10.3 Revue humaine

Avant mise en production, et à chaque changement de profil : **inspection manuelle d'un échantillon d'artefacts de sortie** par une personne qui n'a pas écrit le pipeline. Un `dcmdump` complet, lu ligne par ligne. C'est fastidieux et ça trouve des choses que les tests ne trouvent pas.

Documentez cette revue : date, échantillon, réviseur, conclusion. C'est un livrable pour l'IRB.

---

## 11. Hygiène du poste client

Le poste Slicer détient de la PHI : il est en périmètre HIPAA au même titre que le serveur.

- **Chiffrement intégral du disque** : BitLocker, FileVault ou LUKS. Non négociable.
- **Verrouillage automatique de session** court (§164.312(a)(2)(iii)).
- **Comptes nominatifs**, pas de session partagée « poste de recherche ».
- **Mises à jour de sécurité OS et Slicer** appliquées.
- **Extensions Slicer** : chaque extension installée a accès à toute la scène et à tout le système de fichiers. Restreignez la liste des extensions autorisées sur un poste qui manipule de la PHI.
- **Purge de fin de session** : base DICOM, scènes de récupération, répertoires temporaires, cache d'artefacts.
- **Politique de mise au rebut** conforme NIST SP 800-88.
- **Sauvegardes du poste** : si le poste est sauvegardé vers un service cloud grand public, la PHI part avec. Vérifiez.

---

## 12. Coût en temps et impact utilisateur

| Étape | Coût typique |
|---|---|
| Nettoyage des tags DICOM, headers seuls | 1–5 ms/fichier → **0,5–2 s** pour une série de 300 coupes |
| Nettoyage avec réécriture des pixels (non compressé) | 10–50 ms/coupe → **5–15 s** par série |
| Nettoyage avec réécriture (JPEG2000 lossless) | 50–200 ms/coupe → **15–60 s** par série |
| Conversion DICOM → NIfTI | **2–10 s** par série |
| Remappage des UIDs (HMAC) | < 1 ms/fichier - négligeable |
| `quickshear` | **2–10 s**/volume |
| `pydeface` | **30 s – 3 min**/volume |
| Vérification post-traitement (barrière C) | **0,5–2 s** par série |
| Détection PHI textuelle (`presidio`) | 10–100 ms par champ ; chargement initial du modèle spaCy : **5–15 s** |
| Empreinte SHA-256 de l'artefact | ~0,5 s/Go |

**Ce qu'il faut en retenir :**

1. **Le nettoyage des métadonnées est gratuit** à l'échelle de l'utilisateur (quelques secondes). Aucun arbitrage à faire.
2. **Le defacing domine tout** - c'est le seul poste à optimiser. Il se parallélise très bien (multiprocessing par volume) et se met en cache. Faites-le une fois à l'ingestion, jamais à chaque envoi.
3. **Le chargement des modèles NLP** (`presidio`/spaCy) coûte plus cher que leur exécution. Chargez une fois par session, pas par fichier.
4. **La bonne stratégie UX est de déplacer le coût vers l'ingestion** : l'utilisateur importe et prépare son jeu de données une fois, avec une barre de progression, puis les envois sont instantanés. Le pire design serait d'anonymiser à chaque clic sur « Apply ».

---

## 13. Feuille de route

### Phase 0 - Réglementaire (préalable, 2–4 semaines)
- [ ] Statuer avec le UNC Privacy Office sur le régime applicable **par outil**
- [ ] Protocole IRB (OHRE) couvrant explicitement le transfert vers le serveur de calcul
- [ ] Trancher le cas `surg_mov_pred` : régime B avec DUA, ou Expert Determination
- [ ] Formation CITI de toute l'équipe
- [ ] Confirmer si un service d'honest broker UNC peut porter la table de correspondance

### Phase 1 - Barrière B (le bloquant, 4–6 semaines)
- [ ] Préparateur de charge utile intégré à `run_tool()`, fail-closed, sans chemin alternatif
- [ ] Pipeline PS3.15 récursif + suppression des private tags
- [ ] Remappage HMAC des UIDs avec cohérence relationnelle
- [ ] Politique de dates conforme au régime
- [ ] Renommage neutre des fichiers
- [ ] Vérification post-traitement (barrière C)
- [ ] Manifeste et journal local
- [ ] Clé HMAC dans le trousseau OS

### Phase 2 - Barrière A et durcissement Slicer (4–6 semaines)
- [ ] Module de préparation par lot avec cache
- [ ] Checklist d'ingestion §4.10 automatisée
- [ ] Purge de fin de session (base DICOM, scènes de récupération, temporaires)
- [ ] Politique de logs : aucun chemin d'entrée écrit par votre code
- [ ] Indicateur d'état permanent dans l'UI
- [ ] Defacing si applicable

### Phase 3 - Validation (4 semaines)
- [ ] Suite de tests sur TCIA Pseudo-PHI-DICOM-Data
- [ ] Cas adversariaux (séquences profondes, encodages, private tags constructeur)
- [ ] Test fail-closed avec interception réseau
- [ ] Revue manuelle documentée par un tiers
- [ ] Dossier de validation remis à l'IRB et au Privacy Office

---

## Cinq points à ne pas perdre de vue

1. **Le point d'étranglement est `run_tool()`.** Une seule barrière obligatoire, protégeant les 15+ outils présents et futurs, sans que leurs auteurs aient à y penser.
2. **Fail-closed, sans exception.** Pas de `skip_deid`, pas de mode debug, pas de bouton « envoyer quand même ».
3. **Les fuites Slicer sont aussi dangereuses que les tags DICOM.** Noms de nœuds, logs applicatifs, base DICOM locale, noms de fichiers, scènes de récupération. Un pipeline DICOM parfait ne suffit pas.
4. **`surg_mov_pred` ne peut pas être en Safe Harbor.** Le visage est le signal. Assumez-le, documentez-le, et traitez cet outil dans un régime distinct plutôt que de prétendre le contraire.
5. **Un pipeline non validé ne vaut rien.** TCIA Pseudo-PHI-DICOM-Data, cas adversariaux, revue manuelle par un tiers, dossier remis à l'IRB. C'est la validation, pas le code, qui convainc.

---

*Ce document est une spécification d'ingénierie et ne constitue pas un avis juridique. La qualification du régime applicable (Safe Harbor, Limited Data Set, PHI avec waiver), l'approbation du protocole et la validation du pipeline relèvent du UNC Office of Human Research Ethics et du UNC Privacy Office.*